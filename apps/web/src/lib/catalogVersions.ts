export type CatalogEntry = Record<string, unknown>;
export type CatalogVersion = {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  prompt: string;
  entries: CatalogEntry[];
};

const VERSIONS_KEY = "waypoint-context-catalog-versions";
const LEGACY_KEY = "waypoint-context-catalog-draft";
const SELECTED_KEY = "waypoint-context-catalog-selected";

export function readCatalogVersions(): CatalogVersion[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(VERSIONS_KEY) ?? "[]");
    if (Array.isArray(parsed)) return parsed.filter((item): item is CatalogVersion => item && typeof item.id === "string" && Array.isArray(item.entries));
  } catch { /* treat corrupt local state as empty */ }
  try {
    const legacy = JSON.parse(window.localStorage.getItem(LEGACY_KEY) ?? "{}");
    if (Array.isArray(legacy.entries) && legacy.entries.length > 0) {
      const timestamp = legacy.saved_at ?? new Date().toISOString();
      return [{ id: "legacy", name: "Imported draft", created_at: timestamp, updated_at: timestamp, prompt: "", entries: legacy.entries }];
    }
  } catch { /* treat corrupt legacy state as empty */ }
  return [];
}

export function selectedCatalogVersionId(): string {
  if (typeof window === "undefined") return "fresh";
  try {
    return window.localStorage.getItem(SELECTED_KEY) ?? "fresh";
  } catch {
    return "fresh";
  }
}

export function writeCatalogVersions(versions: CatalogVersion[], selectedId?: string) {
  window.localStorage.setItem(VERSIONS_KEY, JSON.stringify(versions, null, 2));
  if (selectedId) window.localStorage.setItem(SELECTED_KEY, selectedId);
  window.dispatchEvent(new Event("waypoint-catalog-updated"));
}

export function newCatalogVersionId() {
  return `catalog-${Date.now()}`;
}
