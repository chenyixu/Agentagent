import { authSession, client, getBaseUrl } from './client';
import type { AuthConfigResponse } from './types';

const configuredEndpoint = (name: string): string | undefined => {
	const value = import.meta.env[name] as string | undefined;
	return value?.trim() || undefined;
};

export const authApi = {
	/** Discover the server-side auth mode before choosing a browser flow. */
	config: async (baseUrl = getBaseUrl()): Promise<AuthConfigResponse> => {
		return client.get<AuthConfigResponse>('/auth/config', undefined, {
			baseUrl,
			silent: true,
		});
	},

	/** Complete controlled bearer login without persisting the access token. */
	loginWithAccessToken: (token: string): void => {
		authSession.setAccessToken(token);
	},

	/** Apply server mode and deployment-local refresh/logout endpoint settings. */
	configure: (config: AuthConfigResponse): void => {
		authSession.configure({
			mode: config.mode,
			// Refresh and logout are deliberately BFF/OAuth deployment settings.
			// They are not returned by the server because exposing provider URLs
			// is unnecessary for the core contract.
			refreshEndpoint:
				config.mode === 'bearer'
					? configuredEndpoint('VITE_AUTH_REFRESH_ENDPOINT')
					: undefined,
			logoutEndpoint:
				config.mode === 'bearer'
					? configuredEndpoint('VITE_AUTH_LOGOUT_ENDPOINT')
					: undefined,
		});
	},

	/** Renew through an HttpOnly-cookie-backed BFF/OAuth endpoint. */
	refresh: () => authSession.refresh(),

	/** Revoke remotely when configured, then clear all browser identity state. */
	logout: () => authSession.logout(),

	/** Development identity switching is explicit and clears session pointers. */
	switchDevelopmentIdentity: (userId: string): void => {
		authSession.setDevelopmentIdentity(userId);
	},
};
