import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WoCardComponent } from '../../../theme/components';
import { WorkStoreService } from '../../services/work-store.service';
import { ApiStatusComponent } from '../api-status/api-status.component';
import { IconComponent } from '../icon/icon.component';
@Component({
  imports: [WoCardComponent, RouterLink, ApiStatusComponent, IconComponent],
  templateUrl: './overview.component.html',
})
export class OverviewComponent {
  readonly store = inject(WorkStoreService);
}
