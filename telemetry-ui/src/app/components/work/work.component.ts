import { Component, inject } from '@angular/core';
import { WoCardComponent } from '../../../theme/components';
import { WorkStoreService } from '../../services/work-store.service';
import { WorkItemFormComponent } from '../work-item-form/work-item-form.component';
@Component({
  imports: [WoCardComponent, WorkItemFormComponent],
  templateUrl: './work.component.html',
})
export class WorkComponent {
  readonly store = inject(WorkStoreService);
}
