import { useEffect, useState } from "react";
import { api } from "../api.js";

/** What the UI assumes before `/api/info` answers, or when it never does. */
const UNKNOWN = {
  version: "",
  llmAvailable: false,
  effects: null,
  engines: [],
  capabilities: null,
};

/**
 * What this deployment can do, from `GET /api/info`.
 *
 * One state object rather than five. App held `version`, `llmAvailable`, `effects`, `engines` and
 * `capabilities` as five separate `useState` calls that were all written from the same `.then()`
 * and never written anywhere else — five setters firing in sequence off one response, which is one
 * piece of information wearing five hats.
 *
 * **Every default here is "assume capable", and that is deliberate.** These values gate UI
 * controls, and the panel reads absent capability as available (`!== false`) so an older backend
 * that does not advertise an engine does not have working features hidden from it. Defaulting to
 * "nothing works" would make an unreachable `/api/info` look like an install with no features
 * rather than an install that could not be asked — the same failure the repo's degradation contract
 * exists to avoid, in the other direction.
 *
 * Never rejects. A failed probe leaves {@link UNKNOWN} in place, because the app has to remain
 * usable when the info endpoint is the only thing that is broken.
 */
export function useApiInfo() {
  const [info, setInfo] = useState(UNKNOWN);

  useEffect(() => {
    let live = true;
    api
      .info()
      .then((payload) => {
        if (!live) return;
        setInfo({
          version: payload.version || "",
          llmAvailable: !!payload.llm_available,
          effects: payload.effects || null,
          // Guarded because the panel maps over it: a non-list here would throw during render,
          // taking down the whole page over a malformed optional field.
          engines: Array.isArray(payload.engines) ? payload.engines : [],
          capabilities: payload.capabilities || null,
        });
      })
      .catch(() => {});
    // `live` rather than an AbortController: the fetch is harmless to complete, and what has to be
    // prevented is `setInfo` on an unmounted component.
    return () => {
      live = false;
    };
  }, []);

  return info;
}
