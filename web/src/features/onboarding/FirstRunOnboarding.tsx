import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { useAssistantStore } from "@/store";
import { AudioStep } from "./AudioStep";
import { ModelStep } from "./ModelStep";
import { NameStep } from "./NameStep";

type OnboardingStep = "name" | "model" | "audio";

/**
 * Blocking first-run modal: name -> chat model -> audio devices.
 *
 * Shown exactly when ``identity.needs_onboarding`` is true; closes when
 * the backend confirms persistence via the ``identity_changed`` WS
 * broadcast (which flips the gate to false) and the remaining steps are
 * dismissed.
 *
 * The name step is intentionally not dismissable -- every prompt block,
 * transcript formatter, and worker LLM call routes through
 * ``user_display_name``, so letting it be skipped would leak the
 * ``"friend"`` fallback into long-term memory rows. The model and audio
 * steps are skippable: both have a Settings equivalent.
 *
 * A re-opener for renames lives in the General tab of
 * :file:`SettingsDrawer.tsx`; this component only handles the empty-state
 * onboarding path.
 */
export function FirstRunOnboarding() {
  const identity = useAssistantStore((s) => s.identity);
  const setIdentity = useAssistantStore((s) => s.setIdentity);
  const pushToast = useAssistantStore((s) => s.pushToast);

  const missingChatModel = useAssistantStore((s) => s.missingChatModel);
  const setMissingChatModel = useAssistantStore((s) => s.setMissingChatModel);

  const [step, setStep] = useState<OnboardingStep>("name");
  /** True when the modal opened only to fix a missing model, i.e. the
   *  user onboarded long ago and shouldn't be walked through audio. */
  const [gateOnly, setGateOnly] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (identity?.needs_onboarding && step === "name") {
      inputRef.current?.focus();
    }
  }, [identity?.needs_onboarding, step]);

  const submit = useCallback(
    async (event?: React.FormEvent) => {
      event?.preventDefault();
      const cleaned = name.trim();
      if (!cleaned) {
        setError("Please tell Aiko what to call you.");
        inputRef.current?.focus();
        return;
      }
      if (cleaned.length > 32) {
        setError("Keep it under 32 characters.");
        return;
      }
      setSubmitting(true);
      setError(null);
      try {
        const next = await api.setIdentity(cleaned);
        // The WS broadcast usually beats this response, but we still
        // want to advance regardless of which arrives first.
        // ``identity.needs_onboarding`` may already be false here,
        // hence the explicit ``setStep`` below.
        setIdentity(next);
        pushToast("info", `Aiko will call you ${next.user_display_name}.`);
        setStep("model");
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Couldn't save the name.";
        setError(message);
      } finally {
        setSubmitting(false);
      }
    },
    [name, setIdentity, pushToast],
  );

  // An already-onboarded user whose configured model isn't installed
  // (fresh clone, or a model deleted out from under us) gets dropped
  // straight into the model step -- otherwise their first message would
  // just 404 with nothing on screen explaining why.
  const modelGate =
    step === "name" &&
    missingChatModel !== "" &&
    identity != null &&
    !identity.needs_onboarding;
  useEffect(() => {
    if (modelGate) {
      setGateOnly(true);
      setStep("model");
    }
  }, [modelGate]);

  // Leaving the model step: mid-onboarding it hands off to the audio
  // step, but when we jumped straight here for a missing model there's
  // nothing left to show, and "name" is the closed state.
  const leaveModelStep = () => {
    setMissingChatModel("");
    setStep(gateOnly ? "name" : "audio");
  };

  if (
    !identity ||
    (!identity.needs_onboarding && step === "name" && !missingChatModel)
  ) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="first-run-title"
    >
      {step === "name" ? (
        <NameStep
          name={name}
          setName={setName}
          submitting={submitting}
          error={error}
          setError={setError}
          submit={submit}
          inputRef={inputRef}
        />
      ) : step === "model" ? (
        <ModelStep onDone={leaveModelStep} onSkip={leaveModelStep} />
      ) : (
        // Back to "name" is the closed state: the gate is false by now,
        // so the early return above unmounts the modal.
        <AudioStep onDone={() => setStep("name")} />
      )}
    </div>
  );
}
