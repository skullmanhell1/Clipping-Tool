import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";

/**
 * Saved settings profiles: the list, which one is the default, which one is applied, and the four
 * actions over them.
 *
 * Extracted from App, which held these as three `useState` calls plus a `useRef` and four
 * callbacks interleaved with everything else. They are one concern: three of the four actions
 * re-read the list afterwards, and the ref exists only to serve the list.
 *
 * `onApply(profile)` is how a profile reaches the rest of the app. The hook does not own settings
 * or publishing state — a profile *is* a settings blob, and having the profile hook write into
 * settings would make it the owner of both.
 */
export function useProfiles({ settings, publishing, onApply }) {
  const [profiles, setProfiles] = useState([]);
  const [defaultId, setDefaultId] = useState(null);
  const [activeId, setActiveId] = useState("");

  // The default profile pre-fills the form **once**, at startup. Without this guard any later
  // reload of the list — after a save, a delete, or setting a new default — would re-apply the
  // default and silently discard whatever the user had adjusted since.
  const defaultApplied = useRef(false);

  const load = useCallback(async () => {
    try {
      const data = await api.profiles();
      setProfiles(data.profiles || []);
      setDefaultId(data.default_id || null);
      return data;
    } catch {
      // Profiles are a convenience; the app is fully usable without them, so a failure here must
      // not surface as an error the user has to dismiss before creating a clip.
      return null;
    }
  }, []);

  const apply = useCallback(
    (id) => {
      setActiveId(id);
      if (!id) return;
      const profile = profiles.find((item) => item.id === id);
      // A stale id (the profile was deleted in another tab) selects nothing rather than throwing.
      if (profile) onApply(profile);
    },
    [profiles, onApply],
  );

  useEffect(() => {
    let live = true;
    load().then((data) => {
      if (!live || !data?.default_id || defaultApplied.current) return;
      defaultApplied.current = true;
      const profile = (data.profiles || []).find((item) => item.id === data.default_id);
      if (!profile) return;
      setActiveId(profile.id);
      onApply(profile);
    });
    return () => {
      live = false;
    };
    // `onApply` and `load` are stable; re-running this on a new `onApply` identity would re-apply
    // the default over the user's edits, which the ref would then not prevent on a remount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = useCallback(
    async (name, id) => {
      const saved = await api.saveProfile({ name, id, settings, publishing });
      await load();
      setActiveId(saved.id);
    },
    [settings, publishing, load],
  );

  const setDefault = useCallback(
    async (id) => {
      await api.setDefaultProfile(id);
      await load();
    },
    [load],
  );

  const remove = useCallback(
    async (id) => {
      await api.deleteProfile(id);
      // Clear the selection first: leaving a deleted id active would leave the bar showing a
      // profile that no longer exists, with its Delete button still armed.
      if (id === activeId) setActiveId("");
      await load();
    },
    [activeId, load],
  );

  return { profiles, defaultId, activeId, apply, save, setDefault, remove };
}
