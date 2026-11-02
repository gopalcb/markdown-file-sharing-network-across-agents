import { Component, Input, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { WoTextInputComponent } from '../../../theme/components';
import { WorkStoreService } from '../../services/work-store.service';
import { IconComponent } from '../icon/icon.component';

@Component({
  selector: 'wo-work-item-form',
  imports: [ReactiveFormsModule, WoTextInputComponent, IconComponent],
  templateUrl: './work-item-form.component.html',
})
export class WorkItemFormComponent {
  private readonly store = inject(WorkStoreService);
  @Input() prefix = 'work';
  @Input() preview = false;
  readonly submitted = signal(false);
  readonly message = signal('');
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.minLength(3), Validators.maxLength(80)],
    }),
    email: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.email, Validators.maxLength(254)],
    }),
  });
  submit(): void {
    const raw = this.form.getRawValue();

    this.form.setValue({ name: raw.name.trim(), email: raw.email.trim() });

    this.submitted.set(true);

    this.form.markAllAsTouched();

    if (this.form.invalid) {
      this.message.set('Please review the highlighted fields.');

      return;
    }
    const value = this.form.getRawValue();

    if (!this.preview) {
      this.store.add(value.name, value.email);
    }

    this.message.set(
      this.preview ? 'Preview validated. Nothing was saved.' : `Added “${value.name}”.`,
    );

    this.form.reset();

    this.submitted.set(false);
  }
  reset(): void {
    this.form.reset();

    this.submitted.set(false);

    this.message.set('Form reset.');
  }
}
