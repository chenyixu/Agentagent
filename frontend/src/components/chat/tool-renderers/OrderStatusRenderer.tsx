import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { ToolCallWithResult, ToolRenderer } from './types';

type OrderStatus = {
	found?: boolean;
	order_id?: string;
	status?: string;
	status_label?: string;
	updated_at?: string;
	message?: string;
};

function getOrderStatus(pair: ToolCallWithResult): OrderStatus {
	try {
		const parsed: unknown = JSON.parse(getResultText(pair.result));
		return parsed && typeof parsed === 'object' ? (parsed as OrderStatus) : {};
	} catch {
		return {};
	}
}

function statusClass(status?: string): string {
	if (status === 'cancelled' || status === '已取消') {
		return 'bg-red-500/10 text-red-700 dark:text-red-300';
	}
	if (status === 'pending_payment' || status === '待付款') {
		return 'bg-amber-500/10 text-amber-700 dark:text-amber-300';
	}
	if (status === 'processing' || status === '处理中') {
		return 'bg-blue-500/10 text-blue-700 dark:text-blue-300';
	}
	return 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300';
}

export const OrderStatusRenderer: ToolRenderer = {
	getDisplayName: () => '查询订单',

	renderHeader: (pair) => {
		const orderId = parseInput(pair.call.input).order_id;
		return (
			<>
				<span className={toolLabelClass}>查询订单</span>
				<span className={toolArgClass}>
					{typeof orderId === 'string' && orderId.length > 0 ? orderId : '订单号'}
				</span>
			</>
		);
	},

	renderBody: (pair) => {
		if (!pair.result || pair.result.state === 'running') return null;

		const status = getOrderStatus(pair);
		if (status.found === false) {
			return (
				<div className="border rounded-sm bg-background p-3 text-xs">
					<div className="font-medium text-amber-700 dark:text-amber-300">未找到订单</div>
					<div className="mt-1 text-muted-foreground">
						{status.message || '请检查订单号后重试'}
					</div>
				</div>
			);
		}

		if (!status.order_id) return null;

		return (
			<div className="border rounded-sm bg-background p-3 text-xs">
				<div className="flex items-center justify-between gap-3">
					<span className="text-muted-foreground">订单号</span>
					<span className="font-mono font-medium">{status.order_id}</span>
				</div>
				<div className="mt-2 flex items-center justify-between gap-3">
					<span className="text-muted-foreground">当前状态</span>
					<span
						className={`rounded-full px-2 py-0.5 font-medium ${statusClass(status.status)}`}
					>
						{status.status_label || status.status || '未知'}
					</span>
				</div>
				{status.updated_at && (
					<div className="mt-2 flex items-center justify-between gap-3">
						<span className="text-muted-foreground">更新时间</span>
						<span>{status.updated_at}</span>
					</div>
				)}
			</div>
		);
	},
};
