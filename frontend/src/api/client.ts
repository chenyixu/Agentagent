import { toast } from 'sonner';

export const getBaseUrl = () => localStorage.getItem('server_url') ?? '';
export const getUserId = () => localStorage.getItem('username') ?? '';

export type BrowserAuthMode = 'development' | 'bearer';

export interface BrowserAuthConfig {
	mode: BrowserAuthMode;
	refreshEndpoint?: string;
	logoutEndpoint?: string;
}

type AuthListener = () => void;

/** Remove navigation pointers that could select another user's session. */
function clearSessionSelection(): void {
	localStorage.removeItem('chat_last_agent');
	localStorage.removeItem('chat_last_session');
}

/** Short-lived browser auth context; access tokens are never persisted. */
export interface AuthSession {
	getAccessToken: () => string | null;
	setAccessToken: (token: string) => void;
	clear: () => void;
	getMode: () => BrowserAuthMode;
	hasIdentity: () => boolean;
	setDevelopmentIdentity: (userId: string) => void;
	configure: (config: BrowserAuthConfig) => void;
	subscribe: (listener: AuthListener) => () => void;
	refresh: () => Promise<string>;
	logout: () => Promise<void>;
}

let accessToken: string | null = null;
const storedAuthMode = localStorage.getItem('auth_mode');
let authMode: BrowserAuthMode = storedAuthMode === 'bearer' ? 'bearer' : 'development';
let refreshEndpoint: string | undefined;
let logoutEndpoint: string | undefined;
let refreshInFlight: Promise<string> | null = null;
const authListeners = new Set<AuthListener>();

function notifyAuthChanged(): void {
	for (const listener of authListeners) listener();
}

function storeBearerToken(token: string, clearSelection: boolean): string {
	const normalized = token.trim();
	if (!normalized) throw new Error('Access token must not be empty.');
	if (clearSelection && accessToken !== normalized) clearSessionSelection();
	accessToken = normalized;
	authMode = 'bearer';
	localStorage.setItem('auth_mode', authMode);
	localStorage.removeItem('username');
	notifyAuthChanged();
	return normalized;
}

async function refreshBearerToken(): Promise<string> {
	if (refreshInFlight) return refreshInFlight;
	if (authMode !== 'bearer' || !refreshEndpoint) {
		throw new Error('No browser token refresh endpoint is configured.');
	}

	const refresh = (async () => {
		const response = await fetch(refreshEndpoint!, {
			method: 'POST',
			credentials: 'include',
			headers: { Accept: 'application/json' },
		});
		if (!response.ok) throw new Error('The authentication session could not be refreshed.');
		const body = (await response.json()) as { access_token?: unknown };
		if (typeof body.access_token !== 'string' || !body.access_token.trim()) {
			throw new Error('The refresh response did not contain an access token.');
		}
		// Token rotation is not an identity switch; keep the current view.
		return storeBearerToken(body.access_token, false);
	})();
	refreshInFlight = refresh.finally(() => {
		refreshInFlight = null;
	});
	try {
		return await refreshInFlight;
	} catch (error) {
		accessToken = null;
		clearSessionSelection();
		notifyAuthChanged();
		throw error;
	}
}

export const authSession: AuthSession = {
	getAccessToken: () => accessToken,
	setAccessToken: (token: string) => {
		storeBearerToken(token, true);
	},
	clear: () => {
		accessToken = null;
		if (authMode === 'bearer') clearSessionSelection();
		notifyAuthChanged();
	},
	getMode: () => authMode,
	hasIdentity: () => authMode === 'bearer' ? accessToken !== null : !!getUserId().trim(),
	setDevelopmentIdentity: (userId: string) => {
		const normalized = userId.trim();
		if (!normalized) throw new Error('Development identity must not be empty.');
		accessToken = null;
		authMode = 'development';
		localStorage.setItem('username', normalized);
		localStorage.setItem('auth_mode', authMode);
		clearSessionSelection();
		notifyAuthChanged();
	},
	configure: (config: BrowserAuthConfig) => {
		if (authMode !== config.mode) clearSessionSelection();
		authMode = config.mode;
		localStorage.setItem('auth_mode', authMode);
		refreshEndpoint = config.refreshEndpoint;
		logoutEndpoint = config.logoutEndpoint;
		if (config.mode === 'development') accessToken = null;
		notifyAuthChanged();
	},
	subscribe: (listener: AuthListener) => {
		authListeners.add(listener);
		return () => authListeners.delete(listener);
	},
	refresh: async () => {
		return refreshBearerToken();
	},
	logout: async () => {
		const token = accessToken;
		try {
			if (logoutEndpoint) {
				await fetch(logoutEndpoint, {
					method: 'POST',
					credentials: 'include',
					headers: token ? { Authorization: `Bearer ${token}` } : undefined,
				});
			}
		} finally {
			accessToken = null;
			localStorage.removeItem('username');
			localStorage.setItem('auth_mode', authMode);
			clearSessionSelection();
			notifyAuthChanged();
		}
	},
};

/**
 * Structured error thrown for non-2xx HTTP responses.
 * `message` contains the human-readable detail extracted from the backend.
 */
export class ApiError extends Error {
	readonly status: number;
	readonly detail: string;

	constructor(status: number, detail: string) {
		super(detail);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
	}
}

interface RequestOptions {
	method?: string;
	body?: unknown;
	params?: Record<string, string>;
	/** When true, suppresses the automatic error toast. Useful when the caller shows its own inline error UI. */
	silent?: boolean;
	signal?: AbortSignal;
	/** Overrides the stored server URL. Lets the setup page probe an address before persisting it. */
	baseUrl?: string;
	/** Overrides the stored username, for the same reason as `baseUrl`. */
	userId?: string;
	/** Gives up after this many ms and reports {@link TIMEOUT_STATUS}. Off by default — a streaming chat is meant to stay open. */
	timeoutMs?: number;
	/** Additional request headers owned by the calling API adapter. */
	headers?: Record<string, string>;
	/** Internal guard so a failed refresh cannot recursively retry forever. */
	retryAfterAuthRefresh?: boolean;
}

/** Reported when `timeoutMs` elapses. Real 408s come from a server, so either way the request did not complete in time. */
export const TIMEOUT_STATUS = 408;

function buildHeaders(
	hasBody: boolean,
	userId?: string,
	additionalHeaders?: Record<string, string>,
): Record<string, string> {
	const token = authSession.getAccessToken();
	const headers: Record<string, string> = { ...additionalHeaders };
	if (token) {
		// Bearer mode is authoritative; callers cannot replace it with the
		// development identity header or a different Authorization value.
		delete headers['X-User-ID'];
		delete headers['x-user-id'];
		delete headers['Authorization'];
		delete headers['authorization'];
		headers.Authorization = `Bearer ${token}`;
	} else {
		// A configured bearer deployment must never fall back to a stale
		// development identity after logout or token expiry.
		if (authMode === 'development') headers['X-User-ID'] = userId ?? getUserId();
	}
	if (hasBody) headers['Content-Type'] = 'application/json';
	return headers;
}

/** Parse the response body and extract the `detail` field if the backend returned JSON. */
async function extractErrorDetail(res: Response): Promise<string> {
	const text = await res.text();
	try {
		const json = JSON.parse(text) as { detail?: unknown };
		if (typeof json.detail === 'string') return json.detail;
		if (json.detail !== undefined) return JSON.stringify(json.detail);
	} catch {
		// not JSON – fall through
	}
	return text || res.statusText;
}

async function streamRequest(path: string, options: RequestOptions = {}): Promise<Response> {
	const {
		method = 'GET',
		body,
		params,
		signal,
		silent = false,
		baseUrl,
		userId,
		timeoutMs,
		headers,
		retryAfterAuthRefresh = true,
	} = options;
	const url = new URL(path, baseUrl ?? getBaseUrl());
	if (params) {
		Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
	}

	// AbortSignal.timeout aborts with a TimeoutError, which is what lets the
	// catch below tell "too slow" apart from the caller's own cancellation.
	const deadline = timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined;
	const combined =
		deadline && signal ? AbortSignal.any([signal, deadline]) : (deadline ?? signal);

	let res: Response;
	try {
		res = await fetch(url.toString(), {
			method,
			headers: buildHeaders(body !== undefined, userId, headers),
			body: body ? JSON.stringify(body) : undefined,
			signal: combined,
		});
	} catch (e) {
		// An abort is the caller's own doing — pass it through untouched.
		if (e instanceof DOMException && e.name === 'AbortError') throw e;
		// A server that accepts the connection then stalls would otherwise
		// leave the caller waiting forever.
		const timedOut = e instanceof DOMException && e.name === 'TimeoutError';
		// Otherwise fetch only rejects when the request never reached the
		// server: wrong address, DNS failure, refused connection, blocked
		// preflight. Status 0 distinguishes that from any HTTP-level failure.
		const error = timedOut
			? new ApiError(TIMEOUT_STATUS, 'The server took too long to respond.')
			: new ApiError(
					0,
					'Cannot reach the server. Check the server address and your network.',
				);
		if (!silent) toast.error(error.detail);
		throw error;
	}

	if (!res.ok) {
		const safeToRetry = ['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase());
		if (
			res.status === 401 &&
			retryAfterAuthRefresh &&
			safeToRetry &&
			authMode === 'bearer' &&
			refreshEndpoint
		) {
			try {
				await authSession.refresh();
				return streamRequest(path, {
					...options,
					retryAfterAuthRefresh: false,
				});
			} catch {
				// The refresh helper already clears the bearer context.
			}
		}
		if (res.status === 401) authSession.clear();
		const detail = await extractErrorDetail(res);
		const error = new ApiError(res.status, detail);
		if (!silent) toast.error(detail);
		throw error;
	}

	return res;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
	const res = await streamRequest(path, options);
	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

export const client = {
	get: <T>(
		path: string,
		params?: Record<string, string>,
		options?: { silent?: boolean; baseUrl?: string; userId?: string; timeoutMs?: number },
	) => request<T>(path, { method: 'GET', params, ...options }),
	post: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean; headers?: Record<string, string> },
	) =>
		request<T>(path, {
			method: 'POST',
			body,
			params,
			silent: options?.silent,
			headers: options?.headers,
		}),
	patch: <T>(
		path: string,
		body?: unknown,
		params?: Record<string, string>,
		options?: { silent?: boolean },
	) => request<T>(path, { method: 'PATCH', body, params, silent: options?.silent }),
	delete: <T = void>(path: string, params?: Record<string, string>) =>
		request<T>(path, { method: 'DELETE', params }),
	stream: (path: string, options?: RequestOptions) => streamRequest(path, options),
};
