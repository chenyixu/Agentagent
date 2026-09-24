import { AlertCircle, CalendarCheck2, CheckCircle2, LoaderCircle, RefreshCw, Send } from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, client, getUserId } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

interface ProposalContent {
	action?: string;
	service_name?: string;
	duration_minutes?: number;
	start_at?: string;
	end_at?: string;
	amount_minor?: number;
	currency?: string;
	currency_exponent?: number;
}

interface TaskSnapshot {
	task_id: string;
	state: string;
	version: number;
	event_cursor: number;
	store_id?: string | null;
	waiting?: { question?: string; task_version?: number } | null;
	proposal?: { proposal_id: string; version: number; content?: ProposalContent } | null;
	appointment?: {
		appointment_id: string;
		status: string;
		start_at: string;
		end_at: string;
		amount_minor: number;
		currency: string;
		service_snapshot?: { service_name?: string };
	} | null;
}

interface PendingConfirmation {
	proposal_id: string;
	proposal_version: number;
	confirmation_token: string;
	expected_task_version: number;
	expires_at?: string | null;
}

interface MessageResponse {
	task_id: string;
	task_state: string;
	task_version: number;
	reply_text?: string | null;
	clarification_question?: string | null;
	pending_confirmation?: PendingConfirmation | null;
	event_cursor?: number | null;
}

interface OperationResponse {
	operation_id: string;
	status: string;
	action: string;
	appointment_id?: string | null;
}

interface PendingAttempt {
	taskId: string;
	idempotencyKey: string;
	eventId: string;
}

interface TaskEvent {
	sequence: number;
	type: string;
	occurred_at?: string;
}

function readAttempt(key: string): PendingAttempt | null {
	try {
		const value = sessionStorage.getItem(key);
		return value ? (JSON.parse(value) as PendingAttempt) : null;
	} catch {
		return null;
	}
}

function formatDate(value?: string | null): string {
	if (!value) return '—';
	const date = new Date(value);
	if (Number.isNaN(date.getTime())) return value;
	return new Intl.DateTimeFormat('zh-CN', {
		month: 'long',
		day: 'numeric',
		weekday: 'short',
		hour: '2-digit',
		minute: '2-digit',
	}).format(date);
}

function formatMoney(amount?: number, currency = 'CNY', exponent = 2): string {
	if (amount === undefined) return '—';
	return new Intl.NumberFormat('zh-CN', {
		style: 'currency',
		currency,
	}).format(amount / 10 ** exponent);
}

function makeId(prefix: string): string {
	return `${prefix}-${crypto.randomUUID()}`;
}

function parseEventBlock(block: string): { event: string; data: unknown } | null {
	let event = 'message';
	let data = '';
	for (const line of block.split(/\r?\n/)) {
		if (line.startsWith('event:')) event = line.slice(6).trim();
		if (line.startsWith('data:')) data += `${line.slice(5).trim()}\n`;
	}
	if (!data) return null;
	try {
		return { event, data: JSON.parse(data) as unknown };
	} catch {
		return null;
	}
}

export function AppointmentPage() {
	const identityKey = getUserId() || 'authenticated-user';
	const taskStorageKey = `appointment:last-task:${identityKey}`;
	const attemptStorageKey = `appointment:pending-attempt:${identityKey}`;
	const [storeId, setStoreId] = useState('');
	const [taskId, setTaskId] = useState(() => sessionStorage.getItem(taskStorageKey) ?? '');
	const [snapshot, setSnapshot] = useState<TaskSnapshot | null>(null);
	const [credential, setCredential] = useState<PendingConfirmation | null>(null);
	const [attempt, setAttempt] = useState<PendingAttempt | null>(() => readAttempt(attemptStorageKey));
	const [requestText, setRequestText] = useState('');
	const [reply, setReply] = useState('');
	const [error, setError] = useState('');
	const [busy, setBusy] = useState(false);
	const [streamStart, setStreamStart] = useState<{ taskId: string; cursor: number; nonce: number } | null>(null);
	const [events, setEvents] = useState<TaskEvent[]>([]);
	const [displayEventCursor, setDisplayEventCursor] = useState(0);
	const [confirmed, setConfirmed] = useState<OperationResponse | null>(null);
	const eventCursor = useRef(0);

	const refreshSnapshot = useCallback(async (id: string) => {
		const value = await client.get<TaskSnapshot>(`/booking/v1/tasks/${encodeURIComponent(id)}`, undefined, { silent: true });
		setSnapshot(value);
		eventCursor.current = value.event_cursor;
		setDisplayEventCursor(value.event_cursor);
		return value;
	}, []);

	const recoverCredential = useCallback(async (id: string) => {
		const pending = await client.post<PendingConfirmation>(
			`/booking/v1/tasks/${encodeURIComponent(id)}/confirmation-credential`,
			undefined,
			undefined,
			{ silent: true },
		);
		setCredential(pending);
		return pending;
	}, []);

	const recoverOperation = useCallback(async (saved: PendingAttempt) => {
		try {
			const operation = await client.get<OperationResponse>(
				'/booking/v1/operations',
				{ action: 'CREATE', idempotency_key: saved.idempotencyKey },
				{ silent: true },
			);
			if (operation.status === 'SUCCEEDED' || operation.appointment_id) {
				setConfirmed(operation);
				setCredential(null);
				return true;
			}
			setReply(`预约操作状态：${operation.status}。正在核对业务状态。`);
			return false;
		} catch (cause) {
			if (cause instanceof ApiError && cause.status === 404) return false;
			throw cause;
		}
	}, []);

	useEffect(() => {
		let active = true;
		void client
			.get<{ store_id: string }>('/booking/v1/context', undefined, { silent: true })
			.then((context) => {
				if (active) setStoreId(context.store_id);
			})
			.catch((cause: unknown) => {
				if (active) setError(cause instanceof Error ? cause.message : '无法读取门店身份');
			});
		return () => {
			active = false;
		};
	}, []);

	useEffect(() => {
		if (!taskId) return;
		let active = true;
		void (async () => {
			try {
				const saved = readAttempt(attemptStorageKey);
				const operationRecovered = saved?.taskId === taskId && await recoverOperation(saved);
				const current = await refreshSnapshot(taskId);
				if (!active) return;
				if (operationRecovered) return;
				setStreamStart({ taskId, cursor: 0, nonce: Date.now() });
				if (current.state === 'WAITING_CONFIRMATION' && current.proposal) {
					await recoverCredential(taskId);
				}
			} catch (cause) {
				if (active) setError(cause instanceof Error ? cause.message : '恢复预约状态失败');
			}
		})();
		return () => {
			active = false;
		};
	}, [taskId, taskStorageKey, attemptStorageKey, refreshSnapshot, recoverCredential, recoverOperation]);

	useEffect(() => {
		if (!streamStart) return;
		const controller = new AbortController();
		let active = true;
		void (async () => {
			try {
				const response = await client.stream(
					`/booking/v1/tasks/${encodeURIComponent(streamStart.taskId)}/events?after_sequence=${streamStart.cursor}`,
					{ signal: controller.signal, silent: true },
				);
				if (!response.body) return;
				const reader = response.body.getReader();
				const decoder = new TextDecoder();
				let buffer = '';
				while (active) {
					const { value, done } = await reader.read();
					if (done) break;
					buffer += decoder.decode(value, { stream: true });
					let boundary = buffer.search(/\r?\n\r?\n/);
					while (boundary >= 0) {
						const block = buffer.slice(0, boundary);
						const delimiter = buffer.slice(boundary).match(/^\r?\n\r?\n/)?.[0] ?? '\n\n';
						buffer = buffer.slice(boundary + delimiter.length);
						const frame = parseEventBlock(block);
						if (frame?.event === 'RESET_REQUIRED') {
							const current = await refreshSnapshot(streamStart.taskId);
						if (active) setStreamStart({ taskId: streamStart.taskId, cursor: current.event_cursor, nonce: streamStart.nonce + 1 });
						return;
						}
						if (frame?.data && typeof frame.data === 'object' && 'sequence' in frame.data) {
							const next = frame.data as TaskEvent;
							eventCursor.current = Math.max(eventCursor.current, next.sequence);
							setDisplayEventCursor(eventCursor.current);
							setEvents((current) => [...current.filter((item) => item.sequence !== next.sequence), next].slice(-8));
							if (['proposal_published', 'proposal_invalidated', 'appointment_committed', 'task_state_changed'].includes(next.type)) {
								void refreshSnapshot(streamStart.taskId).catch(() => undefined);
							}
						}
						boundary = buffer.search(/\r?\n\r?\n/);
					}
				}
			} catch (cause) {
				if (active && !(cause instanceof DOMException && cause.name === 'AbortError')) {
					setError(cause instanceof Error ? cause.message : '事件流暂时不可用，可刷新任务状态');
				}
			}
		})();
		return () => {
			active = false;
			controller.abort();
		};
	}, [streamStart, refreshSnapshot]);

	const proposal = snapshot?.proposal?.content;
	const isWaitingConfirmation = snapshot?.state === 'WAITING_CONFIRMATION' && !!snapshot.proposal;
	const canSubmitRequest = useMemo(() => !!requestText.trim() && !!storeId && !busy, [requestText, storeId, busy]);

	const submitRequest = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		if (!canSubmitRequest) return;
		setBusy(true);
		setError('');
		setConfirmed(null);
		setCredential(null);
		const startCursor = snapshot?.event_cursor ?? 0;
		try {
			const response = await client.post<MessageResponse>('/booking/v1/messages', {
				client_message_id: makeId('msg'),
				text: requestText.trim(),
				store_id: storeId,
			}, undefined, { silent: true });
			setRequestText('');
			setReply(response.reply_text ?? response.clarification_question ?? '已收到请求。');
			setTaskId(response.task_id);
			sessionStorage.setItem(taskStorageKey, response.task_id);
			setAttempt(null);
			sessionStorage.removeItem(attemptStorageKey);
			const current = await refreshSnapshot(response.task_id);
			if (response.pending_confirmation && current.state === 'WAITING_CONFIRMATION') {
				setCredential(response.pending_confirmation);
			}
			eventCursor.current = startCursor;
			setStreamStart({ taskId: response.task_id, cursor: startCursor, nonce: Date.now() });
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : '提交预约请求失败');
		} finally {
			setBusy(false);
		}
	};

	const restoreStatus = async () => {
		if (!taskId) return;
		setBusy(true);
		setError('');
		try {
			const saved = readAttempt(attemptStorageKey);
			if (saved?.taskId === taskId && await recoverOperation(saved)) {
				await refreshSnapshot(taskId);
				return;
			}
			const lastSeenCursor = eventCursor.current;
			const current = await refreshSnapshot(taskId);
			setStreamStart({
				taskId,
				cursor: Math.min(lastSeenCursor, current.event_cursor),
				nonce: Date.now(),
			});
			if (current.state === 'WAITING_CONFIRMATION' && current.proposal) {
				await recoverCredential(taskId);
				setReply('已恢复待确认方案。请核对详情后，主动点击确认。');
			} else {
				setCredential(null);
			}
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : '查询预约状态失败');
		} finally {
			setBusy(false);
		}
	};

	const confirmBooking = async () => {
		if (!taskId || !snapshot?.proposal || !credential || busy) return;
		setBusy(true);
		setError('');
		let currentAttempt = readAttempt(attemptStorageKey);
		if (!currentAttempt || currentAttempt.taskId !== taskId) {
			currentAttempt = {
				taskId,
				idempotencyKey: makeId('confirm'),
				eventId: makeId('confirm-event'),
			};
			sessionStorage.setItem(attemptStorageKey, JSON.stringify(currentAttempt));
			setAttempt(currentAttempt);
		}
		try {
			const result = await client.post<OperationResponse>('/booking/v1/confirmations', {
				proposal_id: credential.proposal_id,
				proposal_version: credential.proposal_version,
				confirmation_token: credential.confirmation_token,
				client_confirmation_event_id: currentAttempt.eventId,
				idempotency_key: currentAttempt.idempotencyKey,
				expected_task_version: credential.expected_task_version,
			}, undefined, { silent: true });
			setConfirmed(result);
			setCredential(null);
			setReply(result.status === 'SUCCEEDED' ? '预约已确认。' : `预约结果：${result.status}`);
			await refreshSnapshot(taskId);
		} catch (cause) {
			setError('确认请求未得到明确响应，正在保留原请求标识。请先查询状态；不要另起一笔确认。');
			try {
				if (currentAttempt && await recoverOperation(currentAttempt)) {
					await refreshSnapshot(taskId);
				} else {
					const current = await refreshSnapshot(taskId);
					if (current.state === 'WAITING_CONFIRMATION') await recoverCredential(taskId);
				}
			} catch {
				// The original idempotency key remains persisted for a later manual status check.
			}
			if (cause instanceof Error) setReply(cause.message);
		} finally {
			setBusy(false);
		}
	};

	return (
		<div className="flex size-full min-h-0 overflow-y-auto p-2">
			<main className="mx-auto flex min-h-full w-full max-w-5xl flex-col gap-5 rounded-[22px] bg-card p-5 shadow-panel md:p-8">
				<header className="flex items-center gap-3">
					<div className="flex size-11 items-center justify-center rounded-2xl bg-primary/10 text-primary">
						<CalendarCheck2 className="size-5" />
					</div>
					<div>
					<h1 className="text-2xl font-semibold">智能预约</h1>
					<p className="mt-1 text-sm text-muted-foreground">描述服务与时间偏好，确认前先核对预约方案。</p>
					</div>
				</header>

				<form onSubmit={submitRequest} className="flex flex-col gap-3">
					<label htmlFor="appointment-request" className="text-sm font-medium">预约需求</label>
					<textarea
						id="appointment-request"
						value={requestText}
						onChange={(event) => setRequestText(event.target.value)}
						placeholder="例如：想约肩颈护理，周六下午，尽量安排熟悉的技师。"
						rows={3}
						maxLength={8000}
						className="w-full resize-y rounded-xl border border-input bg-background px-4 py-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
					/>
					<div className="flex flex-wrap items-center justify-between gap-3">
						<p className="text-xs text-muted-foreground">门店上下文由当前登录身份从服务端解析。</p>
						<Button type="submit" disabled={!canSubmitRequest}>
							{busy ? <LoaderCircle className="animate-spin" /> : <Send />}
							提交需求
						</Button>
					</div>
				</form>

				{error && (
					<div role="alert" className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
						<AlertCircle className="mt-0.5 size-4 shrink-0" />
						<span>{error}</span>
					</div>
				)}

				{reply && <p aria-live="polite" className="rounded-xl bg-muted/50 px-4 py-3 text-sm">{reply}</p>}

				{snapshot?.waiting?.question && snapshot.state === 'WAITING_USER' && (
					<div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm">
						<p className="font-medium">需要补充信息</p>
						<p className="mt-1 text-muted-foreground">{snapshot.waiting.question}</p>
					</div>
				)}

				{isWaitingConfirmation && (
					<Card className="border-primary/30">
						<CardHeader>
							<CardTitle>请核对预约方案</CardTitle>
							<CardDescription>当前仍是待确认状态；只有点击下方按钮才会提交预约。</CardDescription>
						</CardHeader>
						<CardContent className="grid gap-3 sm:grid-cols-2">
							<Detail label="服务" value={proposal?.service_name ?? '—'} />
							<Detail label="预约时间" value={formatDate(proposal?.start_at)} />
							<Detail label="时长" value={proposal?.duration_minutes ? `${proposal.duration_minutes} 分钟` : '—'} />
							<Detail label="价格" value={formatMoney(proposal?.amount_minor, proposal?.currency, proposal?.currency_exponent)} />
							{credential?.expires_at && <Detail label="方案有效期" value={formatDate(credential.expires_at)} />}
						</CardContent>
						<div className="flex flex-wrap gap-2 px-4 pb-4">
							<Button onClick={confirmBooking} disabled={!credential || busy}>
								{busy ? <LoaderCircle className="animate-spin" /> : <CheckCircle2 />}
								确认预约
							</Button>
							<Button variant="outline" onClick={restoreStatus} disabled={busy}>
								<RefreshCw /> 恢复确认凭据
							</Button>
						</div>
					</Card>
				)}

				{(confirmed || snapshot?.appointment) && (
					<Card className="border-emerald-600/30">
						<CardHeader>
							<CardTitle className="flex items-center gap-2"><CheckCircle2 className="size-5 text-emerald-600" />预约已确认</CardTitle>
							<CardDescription>订单状态以服务端预约记录为准。</CardDescription>
						</CardHeader>
						<CardContent className="grid gap-3 sm:grid-cols-2">
							<Detail label="服务" value={snapshot?.appointment?.service_snapshot?.service_name ?? proposal?.service_name ?? '预约服务'} />
							<Detail label="预约时间" value={formatDate(snapshot?.appointment?.start_at ?? proposal?.start_at)} />
							<Detail label="订单编号" value={snapshot?.appointment?.appointment_id ?? confirmed?.appointment_id ?? confirmed?.operation_id ?? '已提交'} />
							<Detail label="价格" value={formatMoney(snapshot?.appointment?.amount_minor ?? proposal?.amount_minor, snapshot?.appointment?.currency ?? proposal?.currency, proposal?.currency_exponent)} />
						</CardContent>
					</Card>
				)}

				{taskId && (
					<section className="mt-auto border-t pt-4">
						<div className="flex flex-wrap items-center justify-between gap-2">
							<div>
								<p className="text-sm font-medium">任务状态：{snapshot?.state ?? '恢复中'}</p>
				<p className="text-xs text-muted-foreground">任务 {taskId} · 事件游标 {displayEventCursor}</p>
							</div>
							<Button variant="outline" size="sm" onClick={restoreStatus} disabled={busy}><RefreshCw />查询最新状态</Button>
						</div>
						{events.length > 0 && <p className="mt-2 text-xs text-muted-foreground">实时事件：{events.slice(-4).map((item) => item.type).join(' · ')}</p>}
						{attempt && <p className="mt-1 text-xs text-muted-foreground">待核验确认使用原幂等标识；状态不明时请查询，勿另起新确认。</p>}
					</section>
				)}
			</main>
		</div>
	);
}

function Detail({ label, value }: { label: string; value: string }) {
	return (
		<div className="rounded-lg bg-muted/40 px-3 py-2">
			<p className="text-xs text-muted-foreground">{label}</p>
			<p className="mt-1 break-words text-sm font-medium">{value}</p>
		</div>
	);
}
