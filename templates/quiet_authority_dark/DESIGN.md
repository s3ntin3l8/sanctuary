# Design System Specification: The Sanctuary Protocol

## 1. Overview & Creative North Star
**Creative North Star: "The Digital Atrium"**

This design system rejects the frantic, high-density patterns of traditional SaaS in favor of a "Sanctuary" experience. It is designed to feel like a private library at midnight: quiet, authoritative, and profoundly focused. We achieve this by prioritizing "negative space as a luxury" and replacing harsh structural boundaries with tonal depth.

To break the "template" look, the system employs **intentional asymmetry**. Large headers are often offset, and content cards are layered with varying heights to create a sense of architectural rhythm rather than a rigid, soul-less grid. The goal is to make the user feel they are interacting with a custom-crafted editorial piece, not a generic dashboard.

---

## 2. Colors
Our palette is rooted in the deep, ink-like tones of `surface` (#0b1326), punctuated by a bioluminescent `primary` Teal (#57f1db).

### Surface Hierarchy & Nesting
We achieve structure through **Tonal Layering** rather than lines.
- **Base Layer:** `surface` (#0b1326) – Use for the primary background of the application.
- **Sectional Layer:** `surface-container-low` (#131b2e) – Use for large secondary areas or sidebars.
- **Component Layer:** `surface-container` (#171f33) – Use for primary content cards.
- **Elevated Layer:** `surface-container-highest` (#2d3449) – Use for active states or floating modals.

### The Rules of Engagement
*   **The "No-Line" Rule:** 1px solid borders are strictly prohibited for sectioning. Contrast must be achieved through the transition between `surface` and `surface-container-low`. 
*   **The "Glass & Gradient" Rule:** For primary hero moments, use a subtle radial gradient: `primary_container` (#2dd4bf) at 15% opacity fading into the background. Floating elements should utilize `backdrop-filter: blur(12px)` with a semi-transparent `surface_variant` (#2d3449) to create a "frosted slate" effect.
*   **Signature Textures:** Apply a 2% grain overlay or a very subtle linear gradient (Top: `surface_bright` to Bottom: `surface`) on large empty states to prevent "flatness."

---

## 3. Typography
We utilize **Manrope** exclusively. Its geometric yet humanist qualities provide the "Quiet Authority" required for legal-grade readability without the stiffness of a traditional serif.

*   **Display (lg/md):** Use for high-impact landing moments. Set with `letter-spacing: -0.02em` and `font-weight: 800`. This is your "Editorial" voice.
*   **Headline (lg/md/sm):** Reserved for section starts. Always use `on_surface` (#dae2fd). The generous x-height of Manrope ensures these remain legible even at lower weights.
*   **Title (lg/md/sm):** Used for card headers. These should feel intentional and sturdy (`font-weight: 600`).
*   **Body (lg/md/sm):** The workhorse for legal documents. Ensure `line-height` is set to `1.6` for `body-lg` to provide the "breathing room" essential to the sanctuary aesthetic.
*   **Label (md/sm):** Used for metadata and micro-copy. Use `on_surface_variant` (#bacac5) to de-emphasize non-essential information.

---

## 4. Elevation & Depth
Depth in this system is an "Ambient" experience. We do not use shadows to lift objects; we use light to define them.

*   **The Layering Principle:** To "lift" a document card, place it on a `surface-container-low` background and give the card a `surface-container` fill. The 4-6% difference in luminosity creates a sophisticated, natural lift.
*   **Ambient Glows:** Instead of black shadows, use "Teal Halos" for primary actions. A `box-shadow` of `0 10px 30px -10px rgba(45, 212, 191, 0.3)` creates a soft, authoritative glow behind buttons.
*   **The "Ghost Border" Fallback:** If a border is required for accessibility (e.g., input fields), use `outline_variant` (#3c4a46) at **20% opacity**. It should be felt, not seen.
*   **Glassmorphism:** For navigation overlays, use `surface_container_low` at 80% opacity with a `20px` blur. This keeps the user grounded in their previous context.

---

## 5. Components

### Buttons
*   **Primary:** `primary_container` (#2dd4bf) fill with `on_primary` (#003731) text. **Style:** No border, `0.5rem` (lg) corner radius. Add a subtle inner-glow (top-down) for a tactile feel.
*   **Secondary:** `outline` (#859490) ghost style. Text color: `on_surface`.
*   **Interaction:** On hover, the primary button should "bloom"—increasing its glow radius rather than changing its base color significantly.

### Cards & Lists
*   **The Divider Proscription:** Never use `hr` tags or 1px dividers. Separate list items using `12px` of vertical margin and a 2% background shift on hover (`surface_container_high`).
*   **Legal Documents:** Displayed on `surface_container_lowest` (#060e20) to maximize contrast against `on_surface` text, mimicking high-grade ink on dark paper.

### Input Fields
*   **Base:** `surface_container_low` fill.
*   **Focus State:** The "Ghost Border" becomes 100% opaque `primary` (#57f1db) with a `4px` outer soft glow.
*   **Error State:** Use `error` (#ffb4ab) sparingly. The error message should appear in `label-sm` with a `2px` left-hand accent bar.

### Signature Component: The "Authority Header"
A layout pattern where the `display-md` title is positioned in the top-left, while the `body-md` description is pushed to the far right (asymmetric grid). This creates a sophisticated editorial entry point for every page.

---

## 6. Do's and Don'ts

### Do
*   **Do** use extreme whitespace. If you think there is enough margin, double it.
*   **Do** use `primary` teal for "moments of truth" (success states, primary CTAs) only.
*   **Do** ensure all legal text meets a minimum contrast ratio of 7:1 against its specific surface container.

### Don't
*   **Don't** use pure black (#000000) or pure white (#FFFFFF). It breaks the "Sanctuary" atmosphere.
*   **Don't** use sharp 90-degree corners. Stay within the `0.25rem` to `0.75rem` range to maintain the "Soft" authority.
*   **Don't** use standard "Drop Shadows." If a shadow is necessary, it must be tinted with the `background` color to ensure it looks like a natural occlusion of light.