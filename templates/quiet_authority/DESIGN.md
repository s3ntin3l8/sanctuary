# Design System: The Personal Advocate

## 1. Overview & Creative North Star: "The Quiet Sanctuary"
The legal journey is often chaotic, loud, and emotionally draining. This design system is built to be the antithesis of that experience. Our Creative North Star is **"The Quiet Sanctuary"**—an editorial-inspired digital environment that prioritizes "Quiet Authority." 

We move beyond the "app-like" feel of standard management tools by employing high-end editorial techniques: intentional asymmetry, dramatic whitespace, and a departure from the "box-and-border" mentality. The goal is to make the user feel like they are stepping into a private, high-end law office where everything is handled, organized, and serene. We achieve this through **Tonal Layering** rather than structural lines, creating a UI that feels like stacked sheets of premium vellum.

---

## 2. Colors & Tonal Depth
Our palette avoids the harshness of pure black or the "techiness" of vibrant blues. It is rooted in organic, desaturated tones that evoke stability.

### The "No-Line" Rule
**Borders are prohibited.** To define sections, designers must use background color shifts. For example, a global navigation sidebar in `surface-container-low` (#f0f4f6) sits against a main content area of `surface` (#f8fafb). This creates "invisible" boundaries that feel softer and more sophisticated.

### Surface Hierarchy & Nesting
Depth is achieved through the physical stacking of color.
- **Base Layer:** `background` (#f8fafb)
- **Secondary Sectioning:** `surface-container-low` (#f0f4f6)
- **Interactive Elements/Cards:** `surface-container-lowest` (#ffffff)
- **Deep Insets (e.g., Search Bars):** `surface-container-high` (#e1eaec)

### Signature Textures: Glass & Gradients
To avoid a "flat" template look, use **Glassmorphism** for floating overlays (e.g., Modals or Action Sheets). Use `surface` at 80% opacity with a `20px` backdrop blur. 
**The Signature Gradient:** For primary calls to action, use a subtle linear gradient from `primary` (#45636b) to `primary_dim` (#39575f) at a 135-degree angle. This adds "soul" and a tactile, premium weight to the button.

---

## 3. Typography: The Editorial Voice
We use a dual-font strategy to balance authority with empathy.

*   **Display & Headlines (Manrope):** Chosen for its geometric precision and modern "legal" authority. Use `display-lg` (3.5rem) with wide tracking (-0.02em) for dashboard welcomes to establish a sense of space.
*   **Body & Labels (Inter):** The workhorse. Inter provides maximum legibility for complex legal jargon.

**Hierarchy as Empathy:**
- Use `headline-md` (#2a3437) for case titles.
- Use `body-md` (#566164) for descriptions.
- Use `label-sm` (#727d80) in all-caps with +0.05em letter spacing for metadata (e.g., FILE DATES, STATUS).

---

## 4. Elevation & Depth
In "The Quiet Sanctuary," we do not use heavy shadows. We use light.

*   **The Layering Principle:** A `surface-container-lowest` card placed on a `surface-container-low` background provides enough contrast to be perceived as "elevated" without a single drop shadow.
*   **Ambient Shadows:** If an element must "float" (like a FAB or a Menu), use a shadow tinted with the `on_surface` color: `box-shadow: 0 12px 32px -4px rgba(42, 52, 55, 0.06);`.
*   **The Ghost Border:** If accessibility requires a stroke, use `outline_variant` (#a9b4b7) at **15% opacity**. It should be a suggestion of a line, not a boundary.

---

## 5. Components: Soft Precision

### Buttons (The Anchor)
- **Primary:** `primary` gradient, `lg` (1rem) rounded corners. Text is `on_primary`.
- **Secondary:** No background. `ghost-border` (15% opacity `outline`). This feels "private" and less demanding of the user's attention.
- **Tertiary:** Pure text with `primary` color and `label-md` styling.

### Input Fields (The Intake)
Forbid the standard "box" look. Use a `surface-container-high` background with a `bottom-border` only (using `outline_variant` at 30%). When focused, the background should shift to `surface-container-lowest` with a 1px `primary` bottom-border. This mimics the feeling of filling out a high-quality paper form.

### Cards & Lists (The Case File)
- **No Dividers:** Separate list items using the `spacing-4` (1.4rem) token. 
- **Asymmetric Layout:** Within cards, use "Editorial Alignment." Place the most important data (e.g., Case Status) in a `primary_container` chip in the top right, while the headline sits in the bottom left. This breaks the "grid" and feels bespoke.

### The "Personal Advocate" Timeline
A bespoke component for this system. A vertical line using `outline_variant` (20% opacity) with `primary` dots. Completed milestones use a `primary` glow, while upcoming ones are `on_surface_variant`. Use `spacing-16` (5.5rem) between milestones to allow the case history to "breathe."

---

## 6. Do’s and Don’ts

### Do:
- **Do** use `spacing-20` (7rem) or `spacing-24` (8.5rem) at the top of pages. Luxury is defined by wasted space.
- **Do** use "Soft Teal" (`primary`) sparingly. It is a beacon of progress in a sea of calm grays.
- **Do** wrap sensitive data in `surface-container-lowest` containers to make them feel "protected."

### Don’t:
- **Don't** use 100% opaque borders. They create "visual noise" and increase cognitive load.
- **Don't** use standard "Success Green." Use `primary` or `tertiary` tones to indicate completion; we want to remain "calm," not "alarming."
- **Don't** crowd the interface. If a screen feels busy, increase the background-color contrast between sections instead of adding lines.

---

## 7. Tokens Reference Summary

| Token Class | Value | Usage |
| :--- | :--- | :--- |
| **Primary Space** | `spacing-4` (1.4rem) | Standard gutter between elements. |
| **Editorial Space** | `spacing-12` (4rem) | Vertical breathing room between sections. |
| **Corner Radius** | `lg` (1rem) | Standard for cards and primary buttons. |
| **Corner Radius** | `full` (9999px) | For status chips and search bars. |
| **Surface Base** | `#f8fafb` | The "canvas" for the entire experience. |
| **Text Primary** | `#2a3437` | Reserved for headlines and critical labels. |