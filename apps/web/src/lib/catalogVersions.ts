export type CatalogEntry = Record<string, unknown>;
export type CatalogVersion = {
  id: string;
  name: string;
  tag?: "Edited";
  created_at: string;
  updated_at?: string;
  prompt: string;
  entries: CatalogEntry[];
  feature_catalog_version_id?: string | null;
  confidence_threshold?: number;
  excluded_keys?: string[];
};

export type FeatureCatalogEntry = Record<string, string> & { feature: string };
export type FeatureCatalogVersion = {
  id: string;
  name: string;
  source_filename: string;
  created_at: string;
  entries: FeatureCatalogEntry[];
  csv_text: string;
};

const CONTEXT_VERSIONS_KEY = "waypoint-context-catalog-versions";
const FEATURE_VERSIONS_KEY = "waypoint-feature-catalog-versions";
const LEGACY_KEY = "waypoint-context-catalog-draft";
const CONTEXT_SELECTED_KEY = "waypoint-context-catalog-selected";
const FEATURE_SELECTED_KEY = "waypoint-feature-catalog-selected";

function readArray<T>(key: string, guard: (value: unknown) => value is T): T[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return Array.isArray(value) ? value.filter(guard) : [];
  } catch {
    return [];
  }
}

export function readCatalogVersions(): CatalogVersion[] {
  const versions = readArray<CatalogVersion>(
    CONTEXT_VERSIONS_KEY,
    (item): item is CatalogVersion => !!item && typeof item === "object" && "id" in item && typeof item.id === "string" && "entries" in item && Array.isArray(item.entries),
  );
  if (versions.length) return versions;
  if (typeof window === "undefined") return [];
  try {
    const legacy = JSON.parse(window.localStorage.getItem(LEGACY_KEY) ?? "{}");
    if (Array.isArray(legacy.entries) && legacy.entries.length > 0) {
      const timestamp = legacy.saved_at ?? new Date().toISOString();
      return [{ id: "legacy", name: "Imported draft", created_at: timestamp, prompt: "", entries: legacy.entries }];
    }
  } catch { /* corrupt legacy state is empty */ }
  return [];
}

export function readFeatureCatalogVersions(): FeatureCatalogVersion[] {
  return readArray<FeatureCatalogVersion>(
    FEATURE_VERSIONS_KEY,
    (item): item is FeatureCatalogVersion => !!item && typeof item === "object" && "id" in item && typeof item.id === "string" && "entries" in item && Array.isArray(item.entries),
  );
}

export function selectedCatalogVersionId(): string {
  if (typeof window === "undefined") return "fresh";
  try { return window.localStorage.getItem(CONTEXT_SELECTED_KEY) ?? "fresh"; } catch { return "fresh"; }
}

export function selectedFeatureCatalogVersionId(): string {
  if (typeof window === "undefined") return "";
  try { return window.localStorage.getItem(FEATURE_SELECTED_KEY) ?? ""; } catch { return ""; }
}

export function selectCatalogVersion(id: string) {
  window.localStorage.setItem(CONTEXT_SELECTED_KEY, id);
  window.dispatchEvent(new Event("waypoint-catalog-updated"));
}

export function selectFeatureCatalogVersion(id: string) {
  window.localStorage.setItem(FEATURE_SELECTED_KEY, id);
  window.dispatchEvent(new Event("waypoint-catalog-updated"));
}

export function saveCatalogVersion(version: CatalogVersion) {
  const versions = readCatalogVersions();
  if (!versions.some((item) => item.id === version.id)) {
    window.localStorage.setItem(CONTEXT_VERSIONS_KEY, JSON.stringify([...versions, version]));
  }
  selectCatalogVersion(version.id);
}

export function saveFeatureCatalogVersion(version: FeatureCatalogVersion) {
  const versions = readFeatureCatalogVersions();
  if (!versions.some((item) => item.id === version.id)) {
    window.localStorage.setItem(FEATURE_VERSIONS_KEY, JSON.stringify([...versions, version]));
  }
  selectFeatureCatalogVersion(version.id);
}

export function writeCatalogVersions(versions: CatalogVersion[], selectedId?: string) {
  const unique = versions.filter((version, index) => versions.findIndex((item) => item.id === version.id) === index);
  window.localStorage.setItem(CONTEXT_VERSIONS_KEY, JSON.stringify(unique));
  if (selectedId) selectCatalogVersion(selectedId);
}

function rowFingerprint(row: FeatureCatalogEntry | undefined) {
  if (!row) return "";
  return JSON.stringify(Object.fromEntries(Object.entries(row).sort(([a], [b]) => a.localeCompare(b))));
}

export function compareFeatureCatalogVersions(
  before: FeatureCatalogVersion | undefined,
  after: FeatureCatalogVersion,
) {
  const previous = new Map((before?.entries ?? []).map((entry) => [entry.feature, entry]));
  const current = new Map(after.entries.map((entry) => [entry.feature, entry]));
  return {
    added: [...current.keys()].filter((key) => !previous.has(key)).sort(),
    removed: [...previous.keys()].filter((key) => !current.has(key)).sort(),
    changed: [...current.keys()].filter((key) => previous.has(key) && rowFingerprint(previous.get(key)) !== rowFingerprint(current.get(key))).sort(),
  };
}

export function newCatalogVersionId() {
  return `context-${Date.now()}-${crypto.randomUUID()}`;
}
