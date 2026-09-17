import { CircleAlert, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';

import { authApi, authSession, healthApi } from '@/api';
import { ApiError, TIMEOUT_STATUS } from '@/api/client.ts';
import type { AuthConfigResponse, HealthResponse } from '@/api/types.ts';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Button } from '@/components/ui/button.tsx';
import {
	Card,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from '@/components/ui/card.tsx';
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field.tsx';
import { Input } from '@/components/ui/input.tsx';
import { useTranslation } from '@/i18n/useI18n.ts';
import { formatApiErrorForAlert } from '@/lib/api-error.ts';
import { cn } from '@/lib/utils.ts';

interface Props {
	onComplete: () => void;
	className?: string;
}

/** Pull the failing subsystem names out of a 503 body, if it is a health report. */
function notReadyComponents(detail: string): string {
	try {
		const body = JSON.parse(detail) as HealthResponse;
		return Object.entries(body.components ?? {})
			.filter(([, status]) => status === 'not_ready')
			.map(([name]) => name)
			.join(', ');
	} catch {
		return '';
	}
}

export const SetupPage = ({ onComplete, className }: Props) => {
	const { t } = useTranslation();
	const [url, setUrl] = useState(() => localStorage.getItem('server_url') ?? '');
	const [username, setUsername] = useState(() => localStorage.getItem('username') ?? '');
	const [accessToken, setAccessToken] = useState('');
	const [authConfig, setAuthConfig] = useState<AuthConfigResponse | null>(null);
	const [checking, setChecking] = useState(false);
	const [refreshing, setRefreshing] = useState(false);
	const [errorMsg, setErrorMsg] = useState('');

	const discoverAuthMode = async (baseUrl: string) => {
		try {
			const config = await authApi.config(baseUrl);
			setAuthConfig(config);
			return config;
		} catch {
			// The submit path reports a precise error; discovery while the
			// settings page is open is only a convenience.
			return null;
		}
	};

	useEffect(() => {
		if (url.trim()) void discoverAuthMode(url.trim().replace(/\/+$/, ''));
		// The initial probe is intentionally tied to the initial server URL;
		// submit performs a fresh probe for every edited URL.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);

	const describeFailure = (e: unknown): string => {
		if (e instanceof ApiError) {
			// Status 0 means the request never reached a server at all.
			if (e.status === 0) return t('setup.errorUnreachable');
			if (e.status === TIMEOUT_STATUS) return t('setup.errorTimeout');
			// Something answered, but it has no /health — wrong address, or
			// a backend too old to have one.
			if (e.status === 404) return t('setup.errorNotAgentScope');
			if (e.status === 401 || e.status === 422) return t('setup.errorUnauthorized');
			if (e.status === 503) {
				const down = notReadyComponents(e.detail);
				return down ? `${t('setup.errorNotReady')}\n${down}` : t('setup.errorNotReady');
			}
		}
		// A 200 whose body will not parse as JSON: an SPA dev server or a
		// catch-all proxy answering with HTML, not our backend.
		if (e instanceof SyntaxError) return t('setup.errorNotAgentScope');
		return formatApiErrorForAlert(e);
	};

	const handleSubmit = async (e: React.FormEvent) => {
		e.preventDefault();
		const trimmedUrl = url.trim().replace(/\/+$/, '');
		const trimmedName = username.trim();

		setChecking(true);
		setErrorMsg('');
		let identityApplied = false;
		try {
			const config = await discoverAuthMode(trimmedUrl);
			if (!config) throw new Error(t('setup.errorNotAgentScope'));

			if (config.mode === 'bearer') {
				const token = accessToken.trim() || authSession.getAccessToken();
				if (!token) {
					setErrorMsg(t('setup.bearerTokenRequired'));
					return;
				}
				authApi.loginWithAccessToken(token);
				authApi.configure(config);
				identityApplied = true;
			} else {
				authApi.switchDevelopmentIdentity(trimmedName);
				authApi.configure(config);
				identityApplied = true;
			}

			// Persist only after the backend confirms both the address and
			// the identity works, so a failed attempt cannot leave the app
			// holding a config that sends every later page into errors.
			const health = (await healthApi.check(
				trimmedUrl,
				config.mode === 'development' ? trimmedName : '',
			)) as Partial<HealthResponse>;
			// Valid JSON that is not a health report means the address points
			// at some other service that happens to answer 200.
			if (typeof health.version !== 'string' || !health.components) {
				throw new Error(t('setup.errorNotAgentScope'));
			}
			localStorage.setItem('server_url', trimmedUrl);
			onComplete();
		} catch (err) {
			// Identity is staged in memory before the health request so the
			// request carries the intended credentials. If that validation
			// fails, do not leave an unverified identity active in the app.
			if (identityApplied) await authApi.logout().catch(() => undefined);
			setErrorMsg(describeFailure(err));
		} finally {
			setChecking(false);
		}
	};

	const handleRefresh = async () => {
		setRefreshing(true);
		setErrorMsg('');
		try {
			const token = await authApi.refresh();
			setAccessToken(token);
		} catch (err) {
			setErrorMsg(formatApiErrorForAlert(err));
		} finally {
			setRefreshing(false);
		}
	};

	const handleLogout = async () => {
		setErrorMsg('');
		try {
			await authApi.logout();
		} catch (err) {
			setErrorMsg(formatApiErrorForAlert(err));
		}
	};

	return (
		<div className="flex items-center justify-center h-full">
			<div className={cn('flex flex-col gap-6 w-full max-w-sm', className)}>
				<Card>
					<CardHeader>
						<CardTitle>{t('setup.title')}</CardTitle>
						<CardDescription>{t('setup.description')}</CardDescription>
					</CardHeader>
					<CardContent>
						<form onSubmit={handleSubmit}>
							<FieldGroup>
								<Field>
									<FieldLabel htmlFor="server-url-input">
										{t('setup.serverUrl')}
									</FieldLabel>
									<Input
										id="server-url-input"
										type="url"
										placeholder={t('setup.serverUrlPlaceholder')}
										value={url}
										onChange={(e) => setUrl(e.target.value)}
										required
									/>
								</Field>
								<Field>
									<FieldLabel
										htmlFor={
											authConfig?.mode === 'bearer'
												? 'access-token-input'
												: 'username-input'
										}
									>
										{authConfig?.mode === 'bearer'
											? t('setup.accessToken')
											: t('setup.username')}
									</FieldLabel>
									{authConfig?.mode === 'bearer' ? (
										<Input
											id="access-token-input"
											type="password"
											placeholder={t('setup.accessTokenPlaceholder')}
											value={accessToken}
											onChange={(e) => setAccessToken(e.target.value)}
											autoComplete="off"
											required={!authSession.getAccessToken()}
										/>
									) : (
										<Input
											id="username-input"
											type="text"
											placeholder={t('setup.usernamePlaceholder')}
											value={username}
											onChange={(e) => setUsername(e.target.value)}
											required
										/>
									)}
									<FieldDescription>
										{authConfig?.mode === 'bearer'
											? t('setup.bearerTokenHint')
											: t('setup.developmentIdentityHint')}
									</FieldDescription>
								</Field>
								{errorMsg && (
									<Alert variant="destructive">
										<CircleAlert />
										{/* The failing-component list is appended on its
										    own line, so newlines have to survive. */}
										<AlertDescription className="whitespace-pre-wrap">
											{errorMsg}
										</AlertDescription>
									</Alert>
								)}
								<Field>
									<Button type="submit" className="w-full" disabled={checking}>
										{checking && <Loader2 className="size-3.5 animate-spin" />}
										{checking ? t('setup.checking') : t('setup.submit')}
									</Button>
								</Field>
								{authSession.hasIdentity() && (
									<Field orientation="horizontal">
										{authConfig?.mode === 'bearer' && (
											<Button
												type="button"
												variant="outline"
												onClick={handleRefresh}
												disabled={refreshing}
											>
												{refreshing
													? t('setup.refreshing')
													: t('setup.refreshToken')}
											</Button>
										)}
										<Button
											type="button"
											variant="outline"
											onClick={handleLogout}
										>
											{t('setup.logout')}
										</Button>
									</Field>
								)}
							</FieldGroup>
						</form>
					</CardContent>
				</Card>
				<FieldDescription className="px-6 text-center">{t('setup.hint')}</FieldDescription>
			</div>
		</div>
	);
};
