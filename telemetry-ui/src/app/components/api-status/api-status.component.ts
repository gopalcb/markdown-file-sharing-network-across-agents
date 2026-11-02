import { Component, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { WoCardComponent, WoSpinnerComponent } from '../../../theme/components';
import { HealthService } from '../../services/health.service';
import { IconComponent } from '../icon/icon.component';
@Component({
  selector: 'wo-api-status',
  imports: [WoCardComponent, WoSpinnerComponent, IconComponent],
  templateUrl: './api-status.component.html',
})
export class ApiStatusComponent {
  private readonly health = inject(HealthService);
  private readonly destroyRef = inject(DestroyRef);
  private started = false;
  readonly state = signal<'loading' | 'ready' | 'error'>('loading');
  constructor() {
    this.refresh();
  }
  refresh(): void {
    if (this.state() === 'loading' && this.started) {
      return;
    }

    this.started = true;

    this.state.set('loading');

    this.health
      .check()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (response) => {
          const health = response as {
            status?: unknown;
            service?: unknown;
          } | null;

          this.state.set(
            health?.status === 'ok' && health?.service === 'telemetry-api' ? 'ready' : 'error',
          );
        },
        error: () => this.state.set('error'),
      });
  }
}
