import { Component, inject } from '@angular/core';
import { WoCardComponent } from '../../../theme/components';
import { WorkStoreService } from '../../services/work-store.service';
@Component({
  imports: [WoCardComponent],
  templateUrl: './activity.component.html',
})
export class ActivityComponent {
  readonly store = inject(WorkStoreService);
}
