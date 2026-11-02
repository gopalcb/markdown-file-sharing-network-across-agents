import {
  AfterViewInit,
  Component,
  ChangeDetectorRef,
  inject,
  ElementRef,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  ViewChild,
  SimpleChanges,
} from '@angular/core';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { markdown } from '@codemirror/lang-markdown';
import { MarkdownComponent } from '../markdown/markdown.component';

/** CodeMirror editing with the portal's safe, network-free Markdown preview. */
@Component({
  selector: 'wo-markdown-editor',
  imports: [MarkdownComponent],
  templateUrl: './markdown-editor.component.html',
})
export class MarkdownEditorComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() value = '';
  @Input() label = 'Description';
  @Input() disabled = false;
  @Input() editFirst = false;
  @Output() valueChange = new EventEmitter<string>();
  @ViewChild('editor') editor!: ElementRef<HTMLElement>;
  editing = false;
  private view?: EditorView;
  private readonly changeDetector = inject(ChangeDetectorRef);
  private syncingInput = false;
  private readonly policy = new Compartment();
  ngAfterViewInit(): void {
    this.view = new EditorView({
      parent: this.editor.nativeElement,
      state: EditorState.create({
        doc: this.value,
        extensions: [
          basicSetup,
          markdown(),
          EditorView.lineWrapping,
          this.policy.of([
            EditorState.readOnly.of(this.disabled),
            EditorView.editable.of(!this.disabled),
          ]),
          EditorView.contentAttributes.of({
            'aria-label': this.label,
            role: 'textbox',
            'aria-multiline': 'true',
          }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged && !this.syncingInput) {
              this.value = update.state.doc.toString();

              this.valueChange.emit(this.value);

              this.changeDetector.markForCheck();
            }
          }),
        ],
      }),
    });
  }
  ngOnChanges(changes: SimpleChanges): void {
    if (changes['editFirst']) {
      this.editing = this.editFirst && !this.disabled;
    }

    if (this.view && this.value !== this.view.state.doc.toString()) {
      this.syncingInput = true;

      try {
        this.view.dispatch({
          changes: { from: 0, to: this.view.state.doc.length, insert: this.value },
        });
      } finally {
        this.syncingInput = false;
      }
    }

    this.view?.dispatch({
      effects: this.policy.reconfigure([
        EditorState.readOnly.of(this.disabled),
        EditorView.editable.of(!this.disabled),
      ]),
    });

    if (this.disabled) {
      this.editing = false;
    }
  }
  edit(): void {
    if (this.disabled) {
      return;
    }

    this.editing = true;

    setTimeout(() => {
      this.view?.requestMeasure();

      this.view?.focus();
    });
  }
  ngOnDestroy(): void {
    this.view?.destroy();
  }
}
