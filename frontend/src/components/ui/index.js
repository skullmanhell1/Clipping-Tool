// Shared presentational primitives.
//
// `components/` previously held only feature views — several of them 200 to 970 lines — and every
// small reusable control was defined privately inside whichever one needed it first. `Toggle`
// lived in `SettingsPanel.jsx` and was therefore unavailable to `PublishingPanel` and
// `StorageSettings`, which each re-styled a raw checkbox instead.
//
// This layer exists so the next shared control has somewhere to go that is not "the largest file
// that happens to use it". It is deliberately small: only things with no feature knowledge and no
// API calls belong here.
export { default as Dropdown } from "../Dropdown.jsx";
export { default as Toggle } from "./Toggle.jsx";
