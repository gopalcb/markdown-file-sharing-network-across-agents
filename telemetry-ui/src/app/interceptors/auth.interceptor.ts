import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { AuthService } from '../services/auth.service';
export function isPortalApi(url: string, origin: string): boolean {
  const parsed = new URL(url, origin);

  return (
    parsed.origin === origin &&
    !parsed.username &&
    !parsed.password &&
    parsed.pathname.startsWith('/api/')
  );
}
export const authInterceptor: HttpInterceptorFn = (request, next) => {
  const auth = inject(AuthService);

  if (!isPortalApi(request.url, location.origin)) {
    return next(request);
  }
  const authenticated = auth.hasSession();

  return next(
    authenticated
      ? request.clone({
          setHeaders: auth.sessionHeaders(),
          withCredentials: true,
        })
      : request,
  ).pipe(
    catchError((error: HttpErrorResponse) => {
      if (authenticated && (error.status === 401 || error.status === 403)) {
        void auth.reject(error.status);
      }

      return throwError(() => error);
    }),
  );
};
