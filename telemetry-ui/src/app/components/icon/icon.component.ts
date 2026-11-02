import { Component, Input } from '@angular/core';
import { faArrowRight, faCheck, faPlus, faRotateRight } from '@fortawesome/free-solid-svg-icons';
const icons = {
  arrow: faArrowRight,
  check: faCheck,
  plus: faPlus,
  refresh: faRotateRight,
};
@Component({
  selector: 'wo-icon',
  templateUrl: './icon.component.html',
  host: { class: 'inline-icon', 'aria-hidden': 'true' },
})
export class IconComponent {
  @Input() name: keyof typeof icons = 'arrow';
  get viewBox(): string {
    const icon = icons[this.name].icon;

    return `0 0 ${icon[0]} ${icon[1]}`;
  }
  get path(): string {
    const path = icons[this.name].icon[4];

    return Array.isArray(path) ? path.join(' ') : path;
  }
}
