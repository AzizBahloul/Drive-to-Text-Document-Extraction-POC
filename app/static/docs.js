// Boots the API reference. Kept in a file, not inline, so the page's CSP needs no 'unsafe-inline'.

// Same theme as the app (theme.js: the reader's pick, else the system); Swagger UI's dark
// theme hangs off this class.
const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");
const applyScheme = () =>
  document.documentElement.classList.toggle("dark-mode", window.driveToTextTheme.current() === "dark");
applyScheme();
darkScheme.addEventListener("change", applyScheme);

window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  // Off: with deep links Swagger nests a link inside each operation's toggle button, which
  // screen readers announce badly and leaves a tiny touch target (axe nested-interactive).
  deepLinking: false,
  showExtensions: true,
  showCommonExtensions: true,
  validatorUrl: null, // never send the schema to validator.swagger.io
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
});
