# Viewport control

I’m starting from a fixed `viewBox="0 0 1600 900"` so the reconstructed SVG stays on a clean 16:9 grid with predictable editable coordinates.

Fit policy: preserve the source aspect ratio exactly, use `preserveAspectRatio="xMidYMid meet"`, and fit all geometry inside the frame without cropping or stretching. If the silhouette doesn’t naturally touch every edge, I’ll keep the remaining space as intentional margin rather than distort the composition.

