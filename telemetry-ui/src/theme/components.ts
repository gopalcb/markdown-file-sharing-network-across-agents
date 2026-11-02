import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnDestroy,
  inject,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { FormControl, ReactiveFormsModule } from '@angular/forms';

/** Layout-only card. Use a heading level appropriate to the consumer's page. */
@Component({
  selector: 'wo-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'wo-card' },
  template: `
    @if (heading || eyebrow || description) {
      <header class="wo-card-header">
        @if (eyebrow) {
          <p class="wo-eyebrow">{{ eyebrow }}</p>
        }
        @if (heading) {
          @switch (headingLevel) {
            @case (3) {
              <h3 class="wo-card-title">{{ heading }}</h3>
            }
            @case (4) {
              <h4 class="wo-card-title">{{ heading }}</h4>
            }
            @default {
              <h2 class="wo-card-title">{{ heading }}</h2>
            }
          }
        }
        @if (description) {
          <p class="wo-card-description">{{ description }}</p>
        }
      </header>
    }
    <ng-content />
  `,
})
export class WoCardComponent {
  @Input() heading = '';
  @Input() headingLevel: 2 | 3 | 4 = 2;
  @Input() eyebrow = '';
  @Input() description = '';
}

/** Announce one loading message; set announce=false inside an already labeled button. */
@Component({
  selector: 'wo-spinner',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { style: 'display: contents' },
  template: `
    <span
      class="wo-spinner"
      [class.wo-spinner--card]="size === 'card'"
      [class.wo-spinner--page]="size === 'page'"
      [attr.role]="announce ? 'status' : null"
      [attr.aria-hidden]="announce ? null : 'true'"
    >
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
        <circle
          class="wo-spinner-track"
          cx="12"
          cy="12"
          r="9"
          stroke="currentColor"
          stroke-width="2"
        />
        <circle
          class="wo-spinner-arc"
          cx="12"
          cy="12"
          r="9"
          stroke="currentColor"
          stroke-width="2"
        />
      </svg>
      @if (announce) {
        <span class="wo-visually-hidden">{{ label }}</span>
      }
    </span>
  `,
})
export class WoSpinnerComponent {
  @Input() size: 'inline' | 'card' | 'page' = 'inline';
  @Input() label = 'Loading';
  @Input() announce = true;
}

/** Parent owns the non-nullable control, validators, submission and unique ID. */
@Component({
  selector: 'wo-text-input',
  standalone: true,
  imports: [ReactiveFormsModule],
  host: { class: 'wo-field' },
  template: `
    <label class="wo-field-label" [for]="inputId"
      >{{ label }}
      @if (required) {
        <span aria-hidden="true"> *</span>
      }
    </label>
    <input
      [id]="inputId"
      [type]="type"
      [formControl]="control"
      [required]="required"
      [placeholder]="placeholder"
      [autocomplete]="autocomplete"
      [attr.aria-invalid]="showError ? 'true' : null"
      [attr.aria-describedby]="describedBy"
      [attr.aria-required]="required ? 'true' : null"
    />
    @if (hint) {
      <p class="wo-field-hint" [id]="inputId + '-hint'">{{ hint }}</p>
    }
    @if (showError) {
      <p class="wo-field-error" [id]="inputId + '-error'">{{ errorMessage }}</p>
    }
  `,
})
export class WoTextInputComponent implements OnChanges, OnDestroy {
  private readonly changeDetector = inject(ChangeDetectorRef);
  private controlEvents?: Subscription;
  @Input({ required: true }) inputId!: string;
  @Input({ required: true }) label!: string;
  @Input({ required: true }) control!: FormControl<string>;
  @Input() hint = '';
  @Input() placeholder = '';
  @Input() type: 'text' | 'email' | 'password' | 'search' | 'tel' | 'url' = 'text';
  @Input() autocomplete = 'off';
  @Input() required = false;
  @Input() submitted = false;
  @Input() error = '';

  ngOnChanges(): void {
    this.controlEvents?.unsubscribe();

    // Control state can change without a new input reference (markAllAsTouched, reset).
    this.controlEvents = this.control.events.subscribe(() => this.changeDetector.markForCheck());
  }

  ngOnDestroy(): void {
    this.controlEvents?.unsubscribe();
  }

  get showError(): boolean {
    return this.control.invalid && (this.control.touched || this.submitted);
  }

  get describedBy(): string | null {
    return (
      [this.hint ? `${this.inputId}-hint` : '', this.showError ? `${this.inputId}-error` : '']
        .filter(Boolean)
        .join(' ') || null
    );
  }

  get errorMessage(): string {
    if (this.error) {
      return this.error;
    }

    if (this.control.hasError('required')) {
      return `${this.label} is required.`;
    }

    if (this.control.hasError('email')) {
      return 'Enter a valid email address.';
    }
    const minimum = this.control.getError('minlength')?.requiredLength;

    if (minimum) {
      return `Enter at least ${minimum} characters.`;
    }
    const maximum = this.control.getError('maxlength')?.requiredLength;

    if (maximum) {
      return `Enter no more than ${maximum} characters.`;
    }

    return `Check ${this.label.toLowerCase()}.`;
  }
}
