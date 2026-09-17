import { client } from './client';
import type { ChatRequest } from './types';

/**
 * Chat API — fire-and-forget trigger for chat runs.
 *
 * Events produced by the run are delivered via the session's SSE
 * stream endpoint (``GET /sessions/{sid}/stream``), not in the
 * response body of this POST.
 */
export const chatApi = {
	/**
	 * Trigger a chat run for the specified session.
	 *
	 * Accepts user messages, human-in-the-loop confirmation events,
	 * or ``null`` (continue from current state). Returns immediately;
	 * the caller should already be subscribed to the session's SSE
	 * stream to receive the resulting events.
	 *
	 * @param body - The chat request payload.
	 * @param idempotencyKey - Stable opaque token for retries of the same
	 *   user-message request. The backend binds it to the trusted caller scope.
	 * @returns A confirmation object ``{ status, session_id }``.
	 */
	trigger: (body: ChatRequest, idempotencyKey?: string) =>
		client.post<{ status: string; session_id: string }>('/chat/', body, undefined, {
			headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
		}),
};
