import { client } from './client';

export interface RefundOperationStatus {
	operation_id: string;
	order_id: string;
	ticket_id: string;
	amount_minor: number;
	currency: string;
	status: string;
	state_version: number;
	record_version: number;
	execution_attempt_id: string | null;
	reconciliation_required: boolean;
	updated_at: string;
}

export interface RefundOperationStateEvent {
	event_type: 'refund_operation_state_changed';
	event_id: string;
	operation_id: string;
	status: string;
	state_version: number;
	occurred_at: string;
}

export interface RefundOperationStatusList {
	items: RefundOperationStatus[];
}

export const refundApi = {
	status: (operationId: string) =>
		client.get<RefundOperationStatus>(`/refund-proposals/${operationId}`, undefined, {
			silent: true,
		}),
	sessionStatus: (sessionId: string) =>
		client.get<RefundOperationStatusList>(
			`/refund-proposals/session/${sessionId}`,
			undefined,
			{ silent: true },
		),
};
