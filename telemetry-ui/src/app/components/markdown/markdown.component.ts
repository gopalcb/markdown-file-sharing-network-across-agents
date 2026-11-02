import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { Marked } from 'marked';

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[character]!,
  );
}

// Raw HTML is text and images remain alt text: document content cannot run code
// or cause external image requests. Angular also sanitizes the final binding.
const markdown = new Marked({
  gfm: true,
  renderer: {
    html: ({ text }) => escapeHtml(text),
    image: ({ text }) => escapeHtml(text),
    link({ href, title, tokens }) {
      const label = this.parser.parseInline(tokens);

      try {
        const url = new URL(href);

        if (
          !['https:', 'http:', 'mailto:'].includes(url.protocol) ||
          url.username ||
          url.password
        ) {
          return label;
        }

        return `<a href="${escapeHtml(url.href)}"${title ? ` title="${escapeHtml(title)}"` : ''} target="_blank" rel="noopener noreferrer">${label}</a>`;
      } catch {
        return label;
      }
    },
  },
});

/** Shared document/description preview; never bypass Angular HTML sanitization. */
@Component({
  selector: 'wo-markdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './markdown.component.html',
})
export class MarkdownComponent {
  readonly content = input('');
  readonly html = computed(() => {
    const source = this.content().slice(0, 262144);

    try {
      return markdown.parse(source, { async: false });
    } catch {
      return `<pre>${escapeHtml(source)}</pre>`;
    }
  });
}
