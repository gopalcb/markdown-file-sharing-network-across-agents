import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService, safeReturnPath } from '../services/auth.service';

export const authGuard: CanActivateFn = (_route, state) => {
  const auth = inject(AuthService);

  return auth.hasSession()
    ? true
    : inject(Router).createUrlTree(['/work-organizer/login'], {
        queryParams: { returnTo: safeReturnPath(state.url) },
      });
};
