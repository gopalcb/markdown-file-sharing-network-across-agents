import { Component, inject, OnInit } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { WoCardComponent, WoSpinnerComponent } from '../../../theme/components';
import { AuthService } from '../../services/auth.service';

@Component({
  selector: 'wo-auth-page',
  imports: [WoCardComponent, WoSpinnerComponent],
  templateUrl: './auth.component.html',
})
export class AuthComponent implements OnInit {
  readonly auth = inject(AuthService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  readonly mode = this.route.snapshot.data['authMode'];
  readonly returnTo = this.route.snapshot.queryParamMap.get('returnTo');
  readonly heading =
    this.mode === 'denied'
      ? 'Access required'
      : this.mode === 'signed-out'
        ? 'Signed out'
        : this.mode === 'callback'
          ? 'Signing in'
          : 'Sign in to Work Organizer';
  readonly description =
    this.mode === 'denied'
      ? 'Your account does not have access to this workspace. Contact your administrator, or sign out to use another account.'
      : this.mode === 'signed-out'
        ? 'Your portal session has ended.'
        : 'Sign in to organize projects and read your team’s documents. Continue to the secure AWS sign-in page with your application account.';
  ngOnInit(): void {
    if (this.mode === 'callback') {
      void this.auth.finishSignIn();
    }

    if (this.mode === 'signed-out') {
      this.auth.clear();

      void this.router.navigate(['/work-organizer/login'], { replaceUrl: true });
    }
  }
}
