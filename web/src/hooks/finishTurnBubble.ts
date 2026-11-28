import type { useAssistantStore } from "../store";
import type { WsServerEvent } from "../types";

type TurnDone = Extract<WsServerEvent, { type: "turn_done" }>;

export function finishTurnBubble(store: ReturnType<typeof useAssistantStore.getState>, evt: TurnDone) {
  store.finishAssistantBubble();
  if (typeof evt.metrics?.user_message_id === "number") {
    store.stampUserBackendId(evt.metrics.user_message_id);
  }
  const messageId = evt.assistant_message_id;
  store.stampAssistantBackendId(messageId);
  if (evt.delivery_id && typeof messageId === "number" && Number.isInteger(messageId)) {
    return { messageId, deliveryId: evt.delivery_id };
  }
}
