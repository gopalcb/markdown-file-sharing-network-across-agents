export interface AuthConfig {
  issuer: string;
  clientId: string;
  domain: string;
  redirectUri: string;
  logoutUri: string;
  passkeysEnabled?: boolean;
}

export interface AuthIdentity {
  readonly issuer: string;
  readonly sub: string;
  readonly displayName: string;
}

export interface BrowserSession {
  issuer: string;
  sub: string;
  displayName: string;
  expiresAt: number;
  csrfToken: string;
}
