import { Component } from '@angular/core';
import { WoCardComponent } from '../../../theme/components';
import { WorkItemFormComponent } from '../work-item-form/work-item-form.component';
@Component({
  imports: [WoCardComponent, WorkItemFormComponent],
  templateUrl: './forms.component.html',
})
export class FormsComponent {}
