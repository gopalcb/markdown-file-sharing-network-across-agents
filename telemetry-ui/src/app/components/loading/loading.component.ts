import { Component, OnDestroy, signal } from '@angular/core';
import { WoCardComponent, WoSpinnerComponent } from '../../../theme/components';

@Component({
  imports: [WoCardComponent, WoSpinnerComponent],
  templateUrl: './loading.component.html',
})
export class LoadingComponent implements OnDestroy {
  readonly buttonBusy = signal(false);
  readonly cardBusy = signal(false);
  readonly pageBusy = signal(false);
  readonly buttonMessage = signal('Ready when you are.');
  readonly cardMessage = signal('A card keeps its place while loading.');
  readonly pageMessage = signal('Navigation stays available while content loads.');
  private timers: ReturnType<typeof setTimeout>[] = [];
  preview(scope: 'button' | 'card' | 'page'): void {
    const busy =
      scope === 'button' ? this.buttonBusy : scope === 'card' ? this.cardBusy : this.pageBusy;
    const message =
      scope === 'button'
        ? this.buttonMessage
        : scope === 'card'
          ? this.cardMessage
          : this.pageMessage;

    if (busy()) {
      return;
    }

    busy.set(true);

    message.set('Loading preview…');

    this.timers.push(
      setTimeout(() => {
        busy.set(false);

        message.set('Preview complete.');
      }, 1400),
    );
  }
  ngOnDestroy(): void {
    this.timers.forEach((timer) => clearTimeout(timer));
  }
}
