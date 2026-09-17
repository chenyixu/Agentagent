import { RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';

import type { HandoffMessage, HandoffRecord } from '@/api';
import { handoffApi } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

const STATUS_LABEL: Record<HandoffRecord['status'], string> = {
	bot_active: '机器人处理中',
	queued: '待接单',
	human_active: '人工处理中',
	resolved: '已结束',
};

function statusLabel(status: HandoffRecord['status']): string {
	return STATUS_LABEL[status] ?? status;
}

/** Minimal staff console backed entirely by the durable handoff endpoints. */
export function HandoffPage() {
	const [items, setItems] = useState<HandoffRecord[]>([]);
	const [selected, setSelected] = useState<HandoffRecord | null>(null);
	const [messages, setMessages] = useState<HandoffMessage[]>([]);
	const [draft, setDraft] = useState('');
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<string | null>(null);

	const refresh = useCallback(async () => {
		setLoading(true);
		try {
			const response = await handoffApi.queue();
			setItems(response.items);
			setSelected((current) =>
				current
					? response.items.find((item) => item.handoff_id === current.handoff_id) ?? current
					: response.items[0] ?? null,
			);
			setError(null);
		} catch (caught) {
			setError((caught as Error).message);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void refresh();
		const timer = window.setInterval(() => void refresh(), 5_000);
		return () => window.clearInterval(timer);
	}, [refresh]);

	const selectedAgentId = selected?.agent_id ?? null;

	const loadMessages = useCallback(async (record: HandoffRecord) => {
		if (!record.agent_id) {
			setMessages([]);
			return;
		}
		try {
			const response = await handoffApi.messages(record.session_id, record.agent_id);
			setMessages(response.items);
		} catch {
			setMessages([]);
		}
	}, []);

	useEffect(() => {
		if (selected) void loadMessages(selected);
	}, [selected, loadMessages]);

	const selectRecord = (record: HandoffRecord) => {
		setSelected(record);
		setDraft('');
	};

	const claim = async () => {
		if (!selected || !selectedAgentId) return;
		try {
			const record = await handoffApi.claim(selected.session_id, selectedAgentId);
			setSelected(record);
			await refresh();
		} catch (caught) {
			setError((caught as Error).message);
			await refresh();
		}
	};

	const sendReply = async () => {
		if (!selected || !selectedAgentId || !draft.trim()) return;
		try {
			const message = await handoffApi.reply(
				selected.session_id,
				selectedAgentId,
				draft.trim(),
			);
			setMessages((current) => [...current, message]);
			setDraft('');
			setError(null);
		} catch (caught) {
			setError((caught as Error).message);
		}
	};

	const resolve = async () => {
		if (!selected || !selectedAgentId) return;
		try {
			const record = await handoffApi.resolve(selected.session_id, selectedAgentId);
			setSelected(record);
			await refresh();
		} catch (caught) {
			setError((caught as Error).message);
		}
	};

	const resumeBot = async () => {
		if (!selected || !selectedAgentId) return;
		try {
			const record = await handoffApi.resumeBot(selected.session_id, selectedAgentId);
			setSelected(record);
			await refresh();
		} catch (caught) {
			setError((caught as Error).message);
		}
	};

	const selectedMessages = useMemo(() => messages, [messages]);

	return (
		<div className="flex size-full gap-2 p-2">
			<section className="flex w-[22rem] shrink-0 flex-col overflow-hidden rounded-[22px] bg-card p-4 shadow-panel">
				<div className="mb-4 flex items-center justify-between">
					<div>
						<h1 className="text-xl font-semibold">坐席工作台</h1>
						<p className="text-sm text-muted-foreground">只显示当前坐席授权范围内的交接</p>
					</div>
					<Button variant="ghost" size="icon-sm" onClick={() => void refresh()} disabled={loading}>
						<RefreshCw className={loading ? 'animate-spin' : ''} />
					</Button>
				</div>
				{error && <div className="mb-3 rounded-md bg-destructive/10 p-2 text-sm">{error}</div>}
				<div className="flex-1 space-y-2 overflow-auto">
					{items.length === 0 && (
						<div className="py-8 text-center text-sm text-muted-foreground">暂无待处理会话</div>
					)}
					{items.map((record) => (
						<button
							key={record.handoff_id}
							type="button"
							onClick={() => selectRecord(record)}
							className={`w-full rounded-lg border p-3 text-left ${selected?.handoff_id === record.handoff_id ? 'border-primary bg-primary/5' : 'hover:bg-muted/50'}`}
						>
							<div className="flex items-center justify-between gap-2">
								<span className="font-medium">{record.session_id}</span>
								<Badge variant="secondary">{statusLabel(record.status)}</Badge>
							</div>
							<div className="mt-1 text-xs text-muted-foreground">
								客户 {record.customer_id} · 版本 {record.version}
							</div>
						</button>
					))}
				</div>
			</section>

			<section className="flex min-w-0 flex-1 flex-col rounded-[22px] bg-card p-6 shadow-panel">
				{selected ? (
					<>
						<div className="flex items-start justify-between gap-4 border-b pb-4">
							<div>
								<h2 className="text-lg font-semibold">会话 {selected.session_id}</h2>
								<div className="mt-1 text-sm text-muted-foreground">
									租户 {selected.tenant_id} · 客户 {selected.customer_id}
								</div>
							</div>
							<div className="flex gap-2">
								{selected.status === 'queued' && (
									<Button onClick={() => void claim()}>接单</Button>
								)}
								{selected.status === 'human_active' && (
									<Button variant="outline" onClick={() => void resolve()}>结束人工</Button>
								)}
								{selected.status === 'resolved' && (
									<Button variant="outline" onClick={() => void resumeBot()}>恢复机器人</Button>
								)}
							</div>
						</div>
						<div className="flex-1 space-y-3 overflow-auto py-4">
							{selectedMessages.map((message) => (
								<div key={message.message_id} className="rounded-lg bg-muted/40 p-3">
									<div className="mb-1 text-xs text-muted-foreground">
										{message.sender_role === 'staff' ? '坐席' : '客户'} · {message.sender_user_id}
									</div>
									<div className="whitespace-pre-wrap">{message.content}</div>
								</div>
							))}
							{selectedMessages.length === 0 && (
								<div className="text-sm text-muted-foreground">暂无人工消息</div>
							)}
						</div>
						<div className="border-t pt-4">
							<Textarea
								value={draft}
								onChange={(event) => setDraft(event.target.value)}
								placeholder={selected.status === 'human_active' ? '输入人工回复…' : '接单后才能回复'}
								disabled={selected.status !== 'human_active'}
							/>
							<div className="mt-2 flex justify-end">
								<Button onClick={() => void sendReply()} disabled={selected.status !== 'human_active' || !draft.trim()}>
									发送人工回复
								</Button>
							</div>
						</div>
					</>
				) : (
					<div className="flex h-full items-center justify-center text-muted-foreground">选择一个交接会话</div>
				)}
			</section>
		</div>
	);
}
