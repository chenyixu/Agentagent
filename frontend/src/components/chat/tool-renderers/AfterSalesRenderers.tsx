import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { ToolCallWithResult, ToolRenderer } from './types';

type JsonResult = Record<string, unknown>;

function parseResult(pair: ToolCallWithResult): JsonResult {
	try {
		const parsed: unknown = JSON.parse(getResultText(pair.result));
		return parsed && typeof parsed === 'object' ? (parsed as JsonResult) : {};
	} catch {
		return {};
	}
}

function value(result: JsonResult, key: string): string | undefined {
	const item = result[key];
	if (typeof item === 'string' || typeof item === 'number') return String(item);
	return undefined;
}

function BodyCard({ children }: { children: React.ReactNode }) {
	return <div className="border rounded-sm bg-background p-3 text-xs space-y-2">{children}</div>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
	return (
		<div className="flex items-start justify-between gap-3">
			<span className="text-muted-foreground shrink-0">{label}</span>
			<span className="text-right break-words">{children}</span>
		</div>
	);
}

function header(label: string, arg: unknown, fallback: string) {
	return (
		<>
			<span className={toolLabelClass}>{label}</span>
			<span className={toolArgClass}>
				{typeof arg === 'string' && arg.length > 0 ? arg : fallback}
			</span>
		</>
	);
}

export const LogisticsStatusRenderer: ToolRenderer = {
	getDisplayName: () => '查询物流',
	renderHeader: (pair) =>
		header('查询物流', parseInput(pair.call.input).order_id, '订单号'),
	renderBody: (pair) => {
		if (!pair.result || pair.result.state === 'running') return null;
		const result = parseResult(pair);
		return (
			<BodyCard>
				<Field label="订单号">{value(result, 'order_id') ?? '—'}</Field>
				<Field label="物流状态">{value(result, 'status') ?? value(result, 'message') ?? '—'}</Field>
				{value(result, 'delay_days') && (
					<Field label="延误天数">{value(result, 'delay_days')} 天</Field>
				)}
				{value(result, 'carrier') && <Field label="承运商">{value(result, 'carrier')}</Field>}
				{value(result, 'description') && (
					<div className="pt-1 text-muted-foreground">{value(result, 'description')}</div>
				)}
			</BodyCard>
		);
	},
};

export const AfterSalesPolicyRenderer: ToolRenderer = {
	getDisplayName: () => '查询售后政策',
	renderHeader: (pair) =>
		header('查询政策', parseInput(pair.call.input).topic, '售后政策'),
	renderBody: (pair) => {
		if (!pair.result || pair.result.state === 'running') return null;
		const result = parseResult(pair);
		const evidence = Array.isArray(result.evidence)
			? (result.evidence as Array<Record<string, unknown>>)
			: [];
		return (
			<BodyCard>
				<Field label="政策版本">{value(result, 'version') ?? '—'}</Field>
				<Field label="生效时间">{value(result, 'effective_from') ?? '—'}</Field>
				{evidence.map((item, index) => (
					<div key={String(item.evidence_id ?? index)} className="rounded bg-muted/50 p-2">
						<div>{String(item.excerpt ?? '')}</div>
						<div className="mt-1 text-muted-foreground">来源：{String(item.source ?? '—')}</div>
					</div>
				))}
				{evidence.length === 0 && value(result, 'message') && (
					<div className="text-muted-foreground">{value(result, 'message')}</div>
				)}
			</BodyCard>
		);
	},
};

export const RefundEligibilityRenderer: ToolRenderer = {
	getDisplayName: () => '评估补偿资格',
	renderHeader: (pair) =>
		header('评估补偿资格', parseInput(pair.call.input).order_id, '订单号'),
	renderBody: (pair) => {
		if (!pair.result || pair.result.state === 'running') return null;
		const result = parseResult(pair);
		const decision = value(result, 'decision');
		const eligible = decision === 'eligible';
		const amountMinor = result.compensation_amount_minor;
		return (
			<BodyCard>
				<div
					className={`inline-flex rounded-full px-2 py-0.5 font-medium ${
						eligible
							? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
							: 'bg-amber-500/10 text-amber-700 dark:text-amber-300'
					}`}
				>
					{eligible ? '符合条件' : decision === 'ineligible' ? '不符合条件' : '待确定'}
				</div>
				<Field label="实际 / 门槛">
					{value(result, 'observed_delay_days') ?? '—'} / {value(result, 'required_delay_days') ?? '—'} 天
				</Field>
				{typeof amountMinor === 'number' && (
					<Field label="补偿额度">¥{(amountMinor / 100).toFixed(2)}</Field>
				)}
				<Field label="政策版本">{value(result, 'policy_version') ?? '—'}</Field>
				{value(result, 'message') && (
					<div className="pt-1 text-muted-foreground">{value(result, 'message')}</div>
				)}
			</BodyCard>
		);
	},
};

export const TicketCreationRenderer: ToolRenderer = {
	getDisplayName: () => '创建售后工单',
	renderHeader: (pair) =>
		header('创建售后工单', parseInput(pair.call.input).order_id, '订单号'),
	renderBody: (pair) => {
		if (!pair.result || pair.result.state === 'running') return null;
		const result = parseResult(pair);
		return (
			<BodyCard>
				<Field label="工单号">
					<span className="font-mono">{value(result, 'ticket_id') ?? '—'}</span>
				</Field>
				<Field label="订单号">{value(result, 'order_id') ?? '—'}</Field>
				<Field label="状态">{value(result, 'status') ?? value(result, 'reason') ?? '—'}</Field>
				{result.replayed === true && (
					<div className="rounded bg-blue-500/10 px-2 py-1 text-blue-700 dark:text-blue-300">
						幂等重放：复用了原工单，没有重复创建
					</div>
				)}
				{value(result, 'message') && (
					<div className="text-muted-foreground">{value(result, 'message')}</div>
				)}
			</BodyCard>
		);
	},
};
