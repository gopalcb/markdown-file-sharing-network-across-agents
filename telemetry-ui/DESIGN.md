# Telemetry UI design

Telemetry is a compact workspace for projects, documents, and portfolio analytics.
Its existing visual identity and public `/work-organizer/` routes are retained.

Documents show the loaded record count in the page title. Project Tracker keeps
pagination actions in their status columns and shows loading beside each column
count. Portfolio analytics uses current-page selection and an app-owned confirmation
dialog; deletion stays pessimistic and requires a complete server receipt before
rows are removed locally.

## Source ownership

`src/theme/theme.css` owns base tokens and shared layout classes.
`src/theme/telemetry.css` owns the compact telemetry variant, including `tm-*`
composition. `src/styles.css` contains application overrides. Angular loads them
once, in that order. `src/theme/components.ts` owns the standalone card, spinner,
and text-input primitives used by pages. Keep behavior and API access in the
application components and services.

## Visual contract

- Use the system font, pale gray canvas, white compact header/cards, green
  primary actions and active navigation, and visible keyboard focus.
- Preserve 4px radii and base 0.07em tracking. The existing reference-exact
  telemetry typography and circular account trigger are explicit variant rules.
- Keep the shell outside content cards. Header navigation contains Projects,
  Documents, Messages and Analytics; Preferences is in the account popup.
- Keep desktop boards in three status columns and stack them on narrow screens.
  The document list and reader also stack on narrow screens.
- Keep overlays in the browser top layer, constrain menus to the viewport, and
  preserve Escape dismissal, focus restoration and reduced-motion behavior.

## Interaction contract

Use native links for navigation and buttons for actions. Retain labels, focus
indicators, accessible loading status, disabled/busy controls, and explicit error,
empty and retry states. Use the shared spinner and reactive controls. Safe
Markdown rendering and authorization remain application responsibilities.

Work items use the existing editing drawer; uploads use the existing dialog.
Document edits preserve revision/conflict handling. Messages remains a disabled
preview without sending or persistence. The design-system routes demonstrate
the same primitives used by the product.

## Verification

Run `npm run build`, `npm test`, and `npm run check:format:web` from the repository
root. The root browser E2E harness owns desktop/mobile workflow checks; static
formatting and Angular tests alone do not prove private Cognito or persistence.
