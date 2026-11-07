import { useEffect, useRef, useState } from "react";
import { api } from "@/api";
import type { TaskSnapshot } from "@/types";
import { TERMINAL_TASK_STATUSES } from "@/types";
import { useWorkStore } from "./useWorkStore";
import { useWorkController } from "./useWorkController";
import { WorkResultView } from "./WorkResultView";

function stateLabel(task: TaskSnapshot) {
  if (task.phase === "budget_exhausted") return "Allowance reached";
  if (task.phase === "user_pause") return "Paused by you";
  if (task.phase === "waiting_child") return "Operation in progress";
  if (task.phase === "scheduled") return "Waiting to continue";
  if (task.status === "awaiting_input") return "Needs your answer";
  if (task.result?.outcome === "partial") return "Partial result";
  if (task.result?.outcome === "uncertain") return "Needs review";
  return task.status === "done" ? "Completed" : task.status;
}

interface Props { open: boolean; mobile: boolean; docked: boolean; width: number }

export default function WorkDrawer({ open, mobile, docked, width }: Props) {
  const close = useWorkStore((s) => s.close);
  const selected = useWorkStore((s) => s.selected);
  const select = useWorkStore((s) => s.select);
  const text = useWorkStore((s) => s.selected == null ? "" : s.drafts[s.selected] ?? "");
  const draft = useWorkStore((s) => s.draft);
  const work = useWorkController(open);
  const [tab, setTab] = useState("conversation");
  const [mode, setMode] = useState("discussion");
  const [step, setStep] = useState<number | null>(null);
  const [answerTarget, setAnswerTarget] = useState<number | null>(null);
  const panel = useRef<HTMLElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const pendingSend = useRef<{ id: number; text: string; mode: string; key: string } | null>(null);
  const task = work.detail?.task;
  const finished = task ? TERMINAL_TASK_STATUSES.has(task.status) : false;
  const questions = work.detail ? [work.detail.task, ...work.detail.children].filter((t) => t.status === "awaiting_input") : [];
  const question = questions.find((t) => t.id === answerTarget) ?? questions[0];

  useEffect(() => { setStep(null); setAnswerTarget(null); setMode("discussion"); }, [selected]);
  useEffect(() => { follow.current = true; }, [selected]);
  const lastMessage = work.detail?.messages.at(-1)?.id;
  useEffect(() => {
    if (tab === "conversation" && follow.current && conversation.current) {
      conversation.current.scrollTop = conversation.current.scrollHeight;
    }
  }, [lastMessage, tab]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key !== "Tab" || !mobile || !panel.current) return;
      const nodes = Array.from(panel.current.querySelectorAll<HTMLElement>("button:not(:disabled), select, textarea, input, [tabindex='0']"));
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keys);
    return () => { document.removeEventListener("keydown", keys); if (previous?.isConnected) previous.focus(); };
  }, [open, mobile, close]);

  const send = async (answer?: string) => {
    if (!task || !(answer ?? text).trim()) return;
    const content = (answer ?? text).trim();
    const id = task.id;
    const effectiveMode = finished ? "discussion" : mode;
    const sendMode = question ? "answer" : effectiveMode;
    let pending = pendingSend.current;
    if (!pending || pending.id !== id || pending.text !== content || pending.mode !== sendMode) {
      pending = { id, text: content, mode: sendMode, key: crypto.randomUUID() };
      pendingSend.current = pending;
    }
    const accepted = await work.act(() => question
      ? api.answerTask(question.id, content)
      : api.postWorkMessage(id, content, effectiveMode, pending!.key));
    if (accepted) {
      if (useWorkStore.getState().drafts[id] === text) draft(id, "");
      pendingSend.current = null;
    }
  };

  if (!open) return null;
  const placement = mobile ? "fixed inset-0 z-40" : docked ? "relative shrink-0" : "fixed bottom-0 right-0 top-0 z-30 shadow-2xl";
  return <aside id="work-drawer" ref={panel} role={mobile ? "dialog" : "complementary"}
    aria-modal={mobile || undefined} aria-label="Aiko's work" style={{ width: mobile ? undefined : width }}
    className={`${placement} flex h-full min-h-0 flex-col border-l border-white/10 bg-[#14121c] text-ink-100`}>
    <header className="flex shrink-0 items-center justify-between border-b border-white/10 px-4 py-3">
      <div><h2 className="font-semibold">Aiko’s work</h2><p className="text-xs text-ink-100/50">Plans, evidence and a conversation for each job</p></div>
      <button type="button" onClick={close} aria-label="Close work drawer" className="rounded px-3 py-2 hover:bg-white/10">Close</button>
    </header>
    <div className="border-b border-white/10 p-3">
      <label className="block text-xs text-ink-100/60" htmlFor="work-select">Work history</label>
      <select id="work-select" value={selected ?? ""} onChange={(e) => select(Number(e.target.value))}
        className="mt-1 w-full rounded border border-white/15 bg-ink-900 p-2 text-sm">
        {!work.jobs.length && <option value="">No work yet</option>}
        {work.jobs.map((job) => <option key={job.id} value={job.id}>#{job.id} · {stateLabel(job)} · {job.title}</option>)}
      </select>
      {work.hasMore && <button type="button" disabled={work.busy} onClick={() => void work.more()} className="mt-2 text-xs underline">Load older work</button>}
    </div>
    {work.error && <div role="alert" className="m-3 rounded border border-rose-400/30 p-3 text-sm text-rose-200">
      {work.error}<button type="button" onClick={() => void work.act(work.refresh)} className="ml-3 underline">Retry</button></div>}
    {!task ? <p className="p-5 text-sm text-ink-100/60">{selected ? "Loading work…" : "Ask Aiko in your conversation to work on something. Her jobs will appear here."}</p> : <>
      <section className="max-h-[35vh] shrink-0 space-y-2 overflow-y-auto border-b border-white/10 p-4">
        <div className="flex flex-wrap items-center gap-2 text-xs"><span className="rounded bg-white/10 px-2 py-1">{stateLabel(task)}</span>
          {task.work && <span className="text-ink-100/60">{task.work.profile}</span>}</div>
        <h3 className="break-words text-sm font-medium">{task.work?.contract.objective || task.title}</h3>
        {task.last_message && <p className="text-xs text-ink-100/60">{task.last_message}</p>}
        {task.work?.plan?.length ? <details><summary className="cursor-pointer text-xs text-ink-100/70">Plan · {task.work.plan.length} steps</summary>
          <ol className="mt-2 max-h-36 list-decimal space-y-1 overflow-y-auto pl-5 text-xs">{task.work.plan.map((item, i) => <li key={i}>{item}</li>)}</ol>
        </details> : null}
        {!finished && <div className="flex flex-wrap gap-2">
          {task.work && (task.status === "paused" ? <button type="button" disabled={work.busy} onClick={() => void work.act(() => api.controlWork(task.id, "resume"))}
            className="rounded border border-white/20 px-3 py-1 text-xs">{task.phase === "budget_exhausted" ? "Continue with more allowance" : "Resume"}</button>
            : task.status === "running" && <button type="button" disabled={work.busy} onClick={() => void work.act(() => api.controlWork(task.id, "pause"))}
              className="rounded border border-white/20 px-3 py-1 text-xs">Pause</button>)}
          <button type="button" disabled={work.busy} onClick={() => void work.act(() => api.cancelTask(task.id))} className="rounded border border-white/20 px-3 py-1 text-xs">Cancel work</button>
        </div>}
        {task.phase === "budget_exhausted" && <p className="text-xs text-amber-200">Paused at its allowance. Continuing adds a bounded allowance and keeps previous usage.</p>}
      </section>
      <nav aria-label="Work views" className="flex shrink-0 border-b border-white/10 px-3">
        {["conversation", "steps", "result"].map((name) => <button type="button" key={name} aria-pressed={tab === name}
          onClick={() => setTab(name)} className={`px-3 py-3 text-sm capitalize ${tab === name ? "border-b-2 border-pink-300 text-pink-100" : "text-ink-100/50"}`}>{name}</button>)}
      </nav>
      <div ref={conversation} onScroll={(e) => {
        const el = e.currentTarget;
        follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }} className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-4">
        {tab === "conversation" && <div className="space-y-4">
          {work.detail?.has_older && <button type="button" disabled={work.busy} onClick={() => void work.earlier()} className="text-xs underline">Load earlier messages</button>}
          {!work.detail?.messages.length && <p className="text-sm text-ink-100/50">Ask about her approach or give guidance for her next step. This conversation stays with this job.</p>}
          {work.detail?.messages.map((message) => <article key={message.id} className={`rounded-lg border border-white/10 p-3 ${message.data?.role === "user" ? "ml-6 bg-white/5" : "mr-3"}`}>
            <div className="mb-2 flex justify-between gap-2 text-[11px] text-ink-100/50"><span>{message.data?.role === "user" ? "You" : "Aiko"} · {String(message.data?.kind ?? "message")}</span><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div>
            <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{String(message.data?.content ?? "")}</p>
          </article>)}
        </div>}
        {tab === "steps" && <div className="space-y-3">
          {!work.detail?.children.length && <p className="text-sm text-ink-100/50">No operations dispatched yet.</p>}
          {work.detail?.children.map((child) => <section key={child.id} className="rounded-lg border border-white/10 p-3">
            <button type="button" aria-expanded={step === child.id} onClick={() => setStep(step === child.id ? null : child.id)} className="w-full text-left">
              <div className="text-xs text-ink-100/50">#{child.id} · {stateLabel(child)}</div><div className="mt-1 break-words text-sm">{child.title}</div>
              {child.last_message && <p className="mt-1 text-xs text-ink-100/60">{child.last_message}</p>}
            </button>
            {step === child.id && <div className="mt-3 border-t border-white/10 pt-3"><WorkResultView key={`${child.id}:${child.status}`} taskId={child.id} /></div>}
          </section>)}
        </div>}
        {tab === "result" && <div className="space-y-4">
          {task.work?.verification && <div className="rounded border border-white/15 p-3 text-xs">Evidence review: {task.work.verification.status}<p className="mt-1">{task.work.verification.reason}</p></div>}
          {task.work?.contract.criteria?.length ? <details><summary className="cursor-pointer text-xs">Completion criteria</summary><ul className="mt-2 list-disc pl-4 text-xs">{task.work.contract.criteria.map((c, i) => <li key={i}>{c}</li>)}</ul></details> : null}
          <WorkResultView key={`${task.id}:${task.status}`} taskId={task.id} />
          {task.work && <details><summary className="cursor-pointer text-xs">Usage and allowance</summary><p className="mt-2 text-xs text-ink-100/60">{task.work.iteration} planning turns · {task.work.children} operations · {Math.round(task.work.active_seconds)}s active calls · {task.work.tokens} reported tokens</p><p className="mt-1 text-xs text-ink-100/50">User and operation waits do not use the planning time allowance.</p></details>}
        </div>}
      </div>
      {(task.handler_name === "goal_workflow" || question) && <form onSubmit={(e) => { e.preventDefault(); void send(); }} className="max-h-[45dvh] shrink-0 space-y-2 overflow-y-auto border-t border-white/10 p-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        {question ? <div className="space-y-2 rounded border border-amber-300/30 p-3">
          {questions.length > 1 && <select aria-label="Question to answer" value={question.id} onChange={(e) => setAnswerTarget(Number(e.target.value))} className="w-full bg-ink-900 text-xs">{questions.map((q) => <option key={q.id} value={q.id}>#{q.id} {q.title}</option>)}</select>}
          <p className="whitespace-pre-wrap break-words text-sm">{question.input_request?.prompt}</p>
          <div className="flex flex-wrap gap-2">{question.input_request?.options?.map((option) => <button type="button" disabled={work.busy} key={option} onClick={() => void send(option)} className="rounded border border-white/20 px-3 py-2 text-xs">{option}</button>)}</div>
        </div> : <div className="flex items-center gap-3 text-xs">
          <label htmlFor="work-message-mode">Message</label><select id="work-message-mode" value={finished ? "discussion" : mode} onChange={(e) => setMode(e.target.value)} className="rounded border border-white/15 bg-ink-900 p-1">
            <option value="discussion">Ask about work</option><option value="guidance" disabled={finished}>Guide next step</option>
          </select>
        </div>}
        <textarea ref={composer} aria-label={question ? "Answer Aiko" : "Work message"} maxLength={8000} rows={2} value={text} onChange={(e) => draft(task.id, e.target.value)}
          placeholder={question ? "Your answer…" : "Talk with Aiko about this job…"} className="w-full resize-none rounded border border-white/15 bg-white/5 p-3 text-sm outline-none focus:border-pink-300/60"
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} />
        <div className="flex items-center justify-between gap-3"><p className="text-[11px] text-ink-100/50">{question ? "Answers the selected question directly." : mode === "guidance" && !finished ? "Applied before her next execution step." : "Discussion reads saved progress; it does not run operations."}</p>
          <button type="submit" disabled={work.busy || !text.trim()} className="rounded bg-pink-400/20 px-4 py-2 text-sm text-pink-100 disabled:opacity-40">{work.busy ? "Sending…" : question ? "Answer" : "Send"}</button></div>
      </form>}
    </>}
  </aside>;
}
