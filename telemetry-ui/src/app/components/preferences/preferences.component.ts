import { Component, inject } from '@angular/core';
import { WoCardComponent } from '../../../theme/components';
import { AuthService } from '../../services/auth.service';

@Component({
  imports: [WoCardComponent],
  templateUrl: './preferences.component.html',
})
export class PreferencesComponent {
  readonly auth = inject(AuthService);
}
