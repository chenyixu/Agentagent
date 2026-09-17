import { client } from './client';

export type HandoffStatus = 'bot_active' | 'queued' | 'human_active' | 'resolved';

export interface HandoffRecord {
	handoff_id: string;
	tenant_id?: string;
	customer_id?: string;
	session_id: string;
	agent_id?: string | null;
	status: HandoffStatus;
	assigned_staff_id?: string | null;
	reason_code: string;
	version: number;
	created_at: string;
	updated_at: string;
}

export interface HandoffMessage {
	tenant_id?: string;
	customer_id?: string;
	message_id: string;
	session_id: string;
	sender_user_id: string;
	sender_role: 'staff' | 'customer';
	content: string;
	created_at: string;
}

export interface HandoffQueueResponse {
	items: HandoffRecord[];
}

export interface HandoffMessagesResponse {
	items: HandoffMessage[];
}

const params = (agentId: string) => ({ agent_id: agentId });

export const handoffApi = {
	queue: () =>
		client.get<HandoffQueueResponse>('/handoff/queue', undefined, { silent: true }),
	get: (sessionId: string, agentId: string) =>
		client.get<HandoffRecord>(`/handoff/${sessionId}`, params(agentId), { silent: true }),
	messages: (sessionId: string, agentId: string) =>
		client.get<HandoffMessagesResponse>(`/handoff/${sessionId}/messages`, params(agentId), {
			silent: true,
		}),
	request: (sessionId: string, agentId: string) =>
		client.post<HandoffRecord>(`/handoff/${sessionId}/request`, undefined, params(agentId)),
	claim: (sessionId: string, agentId: string) =>
		client.post<HandoffRecord>(`/handoff/${sessionId}/claim`, undefined, params(agentId)),
	reply: (sessionId: string, agentId: string, content: string) =>
		client.post<HandoffMessage>(
			`/handoff/${sessionId}/reply`,
			{ content },
			params(agentId),
		),
	resolve: (sessionId: string, agentId: string) =>
		client.post<HandoffRecord>(`/handoff/${sessionId}/resolve`, undefined, params(agentId)),
	resumeBot: (sessionId: string, agentId: string) =>
		client.post<HandoffRecord>(`/handoff/${sessionId}/resume`, undefined, params(agentId)),
};
