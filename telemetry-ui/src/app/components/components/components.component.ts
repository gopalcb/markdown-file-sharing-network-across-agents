import { Component, signal } from '@angular/core';
import { WoCardComponent } from '../../../theme/components';
import { IconComponent } from '../icon/icon.component';
@Component({
  imports: [WoCardComponent, IconComponent],
  templateUrl: './components.component.html',
})
export class ComponentsComponent {
  readonly message = signal('Choose an action to preview its feedback.');
}
