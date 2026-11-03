import { Injectable, InjectionToken, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import {
  InMemoryWebStorage,
  UserManager,
  UserManagerSettings,
  WebStorageStateStore,
} from 'oidc-client-ts';
import { AuthConfig, AuthIdentity, BrowserSession } from '../interfaces/auth.interface';
import { WorkStoreService } from './work-store.service';

export const AUTH_MANAGER = new InjectionToken<(settings: UserManagerSettings) => UserManager>(
  'OIDC manager factory',
  { providedIn: 'root', factory: () => (settings) => new UserManager(settings) },
);
// Preserve auth_time for the fixed session deadline; the library filters it by default.
export const PROTOCOL_FILTER = ['nbf', 'jti', 'nonce', 'acr', 'amr', 'azp', 'at_hash'];
export const HOME = '/work-organizer/workspace/project-tracker';
const RETURN_PATH =
  /^\/work-organizer\/(?:workspace\/(?:project-tracker(?:\/(?:new|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))?|overview|documents|tasks|activity)|design-system\/(?:components|grid|forms|loading)|settings\/preferences)$/;

export function safeReturnPath(value: unknown): string {
  return typeof value === 'string' && value === value.trim() && RETURN_PATH.test(value)
    ? value
    : HOME;
}
export function validateConfig(value: AuthConfig, origin: string): AuthConfig {
  const issuer = new URL(value.issuer);
  const domain = new URL(value.domain);

  if (
    !/^https:\/\/cognito-idp\.[a-z0-9-]+\.amazonaws\.com\/[a-z0-9-]+_[A-Za-z0-9]+$/.test(
      value.issuer,
    ) ||
    !/^[a-z0-9]+$/.test(value.clientId) ||
    (value.passkeysEnabled !== undefined && typeof value.passkeysEnabled !== 'boolean') ||
    domain.protocol !== 'https:' ||
    domain.origin !== value.domain ||
    !domain.hostname.endsWith('.amazoncognito.com') ||
    issuer.search ||
    issuer.hash
  ) {
    throw new Error('Invalid authentication configuration');
  }

  for (const [uri, path] of [
    [value.redirectUri, '/work-organizer/auth/callback'],
    [value.logoutUri, '/work-organizer/signed-out'],
  ]) {
    const url = new URL(uri);

    if (
      url.origin !== origin ||
      url.pathname !== path ||
      url.search ||
      url.hash ||
      url.username ||
      url.password ||
      !(
        url.protocol === 'https:' ||
        (url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))
      )
    ) {
      throw new Error('Invalid authentication return URL');
    }
  }

  return value;
}

class AccessDenied extends Error {}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly createManager = inject(AUTH_MANAGER);
  private readonly router = inject(Router);
  private readonly work = inject(WorkStoreService);
  private manager?: UserManager;
  private settings?: UserManagerSettings;
  private config?: AuthConfig;
  private callbackUrl?: string;
  private epoch = 0;
  private absoluteAt = 0;
  private csrf = '';
  private exchange?: Promise<BrowserSession>;
  private readonly account = signal<AuthIdentity | null>(null);
  readonly identity = this.account.asReadonly();
  readonly displayName = computed(() => this.identity()?.displayName ?? 'Portal account');
  readonly passkeysAvailable = signal(false);
  readonly allowed = signal(false);
  readonly ready = signal(false);
  readonly busy = signal(false);
  readonly message = signal('');

  async initialize(): Promise<void> {
    const epoch = this.epoch;

    if (location.pathname === '/work-organizer/auth/callback') {
      this.callbackUrl = location.href;

      history.replaceState(null, '', location.pathname);
    }

    try {
      const response = await fetch('/auth-config.json', {
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      });

      if (!response.ok) {
        throw new Error('Configuration unavailable');
      }

      this.config = validateConfig(await response.json(), location.origin);

      this.passkeysAvailable.set(this.config.passkeysEnabled === true);

      this.settings = {
        authority: this.config.issuer,
        client_id: this.config.clientId,
        redirect_uri: this.config.redirectUri,
        response_type: 'code',
        scope: 'openid email',
        automaticSilentRenew: false,
        filterProtocolClaims: PROTOCOL_FILTER,
        monitorSession: false,
        loadUserInfo: false,
        disablePKCE: false,
        requestTimeoutInSeconds: 10,
        staleStateAgeInSeconds: 300,
        userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
        stateStore: new WebStorageStateStore({ store: sessionStorage, prefix: 'wo.oidc.' }),
        metadata: {
          issuer: this.config.issuer,
          authorization_endpoint: `${this.config.domain}/oauth2/authorize`,
          token_endpoint: `${this.config.domain}/oauth2/token`,
          jwks_uri: `${this.config.issuer}/.well-known/jwks.json`,
        },
      };

      this.manager = this.createManager(this.settings);

      await this.manager.clearStaleState();

      if (!this.callbackUrl) {
        const restored = await fetch('/api/session', {
          headers: { 'X-Device-Id': this.deviceId() },
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'error',
          signal: AbortSignal.timeout(10000),
        });

        if (restored.ok) {
          const view = await restored.json();

          if (epoch === this.epoch) {
            this.acceptSession(view);
          }
        } else if (restored.status === 403) {
          await this.reject(403);
        } else if (restored.status !== 401) {
          throw new Error('Session unavailable');
        }
      }

      this.ready.set(true);
    } catch {
      this.message.set('Sign-in is unavailable. Please try again.');
    }
  }

  async signIn(returnTo: unknown = HOME): Promise<void> {
    await this.startRedirect(this.manager, returnTo);
  }

  async registerPasskey(): Promise<void> {
    if (!this.hasSession() || !this.passkeysAvailable() || !this.settings || !this.config) {
      return;
    }
    const registration = this.createManager({
      ...this.settings,
      metadata: {
        ...this.settings.metadata!,
        authorization_endpoint: `${this.config.domain}/passkeys/add`,
      },
    });

    await this.startRedirect(registration, '/work-organizer/settings/preferences');
  }

  private async startRedirect(manager: UserManager | undefined, returnTo: unknown): Promise<void> {
    if (!manager || this.busy()) {
      return;
    }
    const epoch = this.epoch;

    this.busy.set(true);

    this.message.set('');

    try {
      await manager.clearStaleState();

      if (epoch === this.epoch) {
        await manager.signinRedirect({
          state: { returnTo: safeReturnPath(returnTo) },
          nonce: crypto.randomUUID(),
        });
      }
    } catch {
      this.message.set('Unable to start sign-in. Please try again.');
    } finally {
      this.busy.set(false);
    }
  }

  async finishSignIn(): Promise<void> {
    const url = this.callbackUrl;

    this.callbackUrl = undefined;
    const epoch = this.epoch;

    if (!url || !this.manager) {
      this.message.set('This sign-in link is no longer valid. Please sign in again.');

      await this.router.navigate(['/work-organizer/login'], { replaceUrl: true });

      return;
    }

    this.busy.set(true);

    try {
      const user = await this.manager.signinRedirectCallback(url);

      if (epoch !== this.epoch) {
        return;
      }
      const origin = user.profile.auth_time;

      if (
        user.expired ||
        !user.access_token ||
        !user.refresh_token ||
        !Number.isInteger(origin) ||
        typeof origin !== 'number' ||
        origin <= 0 ||
        origin > Date.now() / 1000 + 10 ||
        Date.now() >= (origin + 90 * 86400) * 1000
      ) {
        throw new Error('Invalid authentication');
      }

      this.exchange = this.establishSession(user.access_token, user.refresh_token);
      const view = await this.exchange;

      if (epoch !== this.epoch) {
        await this.closeSession(view.csrfToken);

        return;
      }

      if (view.sub !== user.profile.sub || view.expiresAt !== origin + 90 * 86400) {
        await this.closeSession(view.csrfToken);
        throw new Error('Session identity changed');
      }

      this.acceptSession(view);

      await this.manager.removeUser();

      await this.router.navigateByUrl(
        safeReturnPath((user.state as { returnTo?: unknown })?.returnTo),
        { replaceUrl: true },
      );
    } catch (error) {
      if (epoch !== this.epoch) {
        return;
      }

      this.clear();

      this.message.set(
        'Sign-in could not be completed. Please try again or contact your administrator.',
      );

      await this.router.navigate(
        [error instanceof AccessDenied ? '/work-organizer/denied' : '/work-organizer/login'],
        { replaceUrl: true },
      );
    } finally {
      await this.manager?.removeUser();

      this.exchange = undefined;

      this.busy.set(false);
    }
  }

  private async establishSession(access: string, refresh: string): Promise<BrowserSession> {
    const response = await fetch('/api/session', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${access}`,
        'Content-Type': 'application/json',
        'X-Device-Id': this.deviceId(),
      },
      body: JSON.stringify({ refresh_token: refresh }),
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(10000),
    });

    if (response.status === 403) {
      throw new AccessDenied();
    }

    if (!response.ok) {
      throw new Error('Session rejected');
    }

    return response.json();
  }

  private acceptSession(view: BrowserSession): void {
    if (
      !view ||
      view.issuer !== this.config?.issuer ||
      typeof view.sub !== 'string' ||
      !view.sub ||
      typeof view.displayName !== 'string' ||
      !view.displayName ||
      view.displayName.length > 160 ||
      /[\u0000-\u001f\u007f]/.test(view.displayName) ||
      !Number.isInteger(view.expiresAt) ||
      view.expiresAt * 1000 <= Date.now() ||
      view.expiresAt > Date.now() / 1000 + 90 * 86400 + 10 ||
      typeof view.csrfToken !== 'string' ||
      !/^[A-Za-z0-9_-]{43}$/.test(view.csrfToken)
    ) {
      throw new Error('Invalid session');
    }

    this.absoluteAt = view.expiresAt * 1000;

    this.csrf = view.csrfToken;

    this.account.set(
      Object.freeze({ issuer: view.issuer, sub: view.sub, displayName: view.displayName }),
    );

    this.allowed.set(true);
  }

  hasSession(): boolean {
    return this.allowed() && this.identity() !== null && Date.now() < this.absoluteAt;
  }

  sessionHeaders(): Record<string, string> {
    return { 'X-Device-Id': this.deviceId(), 'X-CSRF-Token': this.csrf };
  }

  deviceId(): string {
    const key = 'wo.device-id';
    let id = localStorage.getItem(key);

    if (!id || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id)) {
      id = crypto.randomUUID();

      localStorage.setItem(key, id);
    }

    return id;
  }

  clear(): void {
    this.epoch++;

    this.allowed.set(false);

    this.account.set(null);

    this.absoluteAt = 0;

    this.csrf = '';

    this.work.reset();

    void this.manager?.removeUser();

    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith('wo.oidc.')) {
        sessionStorage.removeItem(key);
      }
    }
  }

  async reject(status: number): Promise<void> {
    this.clear();

    this.message.set(
      status === 403
        ? 'Your account does not have portal access.'
        : 'Your session ended. Sign in to continue.',
    );

    await this.router.navigate([
      status === 403 ? '/work-organizer/denied' : '/work-organizer/login',
    ]);
  }

  private async closeSession(csrf: string): Promise<void> {
    if (!csrf) {
      // Access removal must not prevent signing out after a page restart.
      const form = await fetch('/api/session/logout', {
        headers: { 'X-Device-Id': this.deviceId() },
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      });

      if (form.status === 401) {
        return;
      }

      if (!form.ok) {
        throw new Error('Logout unavailable');
      }

      csrf = (await form.json()).csrfToken;

      if (typeof csrf !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(csrf)) {
        throw new Error('Invalid logout state');
      }
    }
    const response = await fetch('/api/session/logout', {
      method: 'POST',
      headers: { 'X-Device-Id': this.deviceId(), 'X-CSRF-Token': csrf },
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(10000),
    });

    if (!response.ok && response.status !== 401) {
      throw new Error('Logout unavailable');
    }
  }

  async logout(): Promise<void> {
    this.epoch++;

    this.busy.set(true);

    try {
      const pending = this.exchange;
      const view = pending ? await pending : undefined;

      await this.closeSession(view?.csrfToken ?? this.csrf);

      this.clear();

      if (!this.config) {
        await this.router.navigate(['/work-organizer/login']);

        return;
      }
      const url = new URL('/logout', this.config.domain);

      url.searchParams.set('client_id', this.config.clientId);

      url.searchParams.set('logout_uri', this.config.logoutUri);

      location.assign(url.href);
    } catch {
      this.message.set('Sign-out could not be completed. Check your connection and try again.');
    } finally {
      this.busy.set(false);
    }
  }
}
