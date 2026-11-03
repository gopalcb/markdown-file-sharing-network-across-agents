import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WoCardComponent } from '../../../theme/components';
@Component({
  imports: [WoCardComponent, RouterLink],
  templateUrl: './not-found.component.html',
})
export class NotFoundComponent {}
