import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  compareFeatureCatalogVersions,
  readCatalogVersions,
  readFeatureCatalogVersions,
  saveCatalogVersion,
  saveFeatureCatalogVersion,
  selectFeatureCatalogVersion,
  selectedFeatureCatalogVersionId,
  type CatalogVersion,
  type FeatureCatalogVersion,
} from "./catalogVersions";

const featureVersion = (overrides: Partial<FeatureCatalogVersion> = {}): FeatureCatalogVersion => ({
  id: "features-one",
  name: "Features one",
  source_filename: "features.csv",
  created_at: "2026-09-15T00:00:00Z",
  entries: [{ feature: "jobs", description: "Jobs" }],
  csv_text: "feature,description\njobs,Jobs\n",
  ...overrides,
});

const contextVersion = (overrides: Partial<CatalogVersion> = {}): CatalogVersion => ({
  id: "context-one",
  name: "Context one",
  created_at: "2026-09-15T00:00:00Z",
  prompt: "prompt",
  entries: [],
  feature_catalog_version_id: "features-one",
  confidence_threshold: 0.8,
  excluded_keys: ["UNCERTAIN_OVERFLOW"],
  ...overrides,
});

describe("catalog versions", () => {
  beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
    });
  });

  it("never overwrites an immutable feature or context version", () => {
    saveFeatureCatalogVersion(featureVersion());
    saveFeatureCatalogVersion(featureVersion({ name: "Changed name" }));
    saveCatalogVersion(contextVersion());
    saveCatalogVersion(contextVersion({ name: "Changed context" }));

    expect(readFeatureCatalogVersions()).toHaveLength(1);
    expect(readFeatureCatalogVersions()[0].name).toBe("Features one");
    expect(readCatalogVersions()).toHaveLength(1);
    expect(readCatalogVersions()[0].name).toBe("Context one");
    expect(readCatalogVersions()[0].confidence_threshold).toBe(0.8);
    expect(readCatalogVersions()[0].excluded_keys).toEqual(["UNCERTAIN_OVERFLOW"]);
  });

  it("compares exact feature keys and identifies changed rows", () => {
    const before = featureVersion({ entries: [{ feature: "jobs", description: "Old" }, { feature: "voip" }] });
    const after = featureVersion({ id: "features-two", entries: [{ feature: "jobs", description: "New" }, { feature: "hcp_assist" }] });

    expect(compareFeatureCatalogVersions(before, after)).toEqual({
      added: ["hcp_assist"],
      removed: ["voip"],
      changed: ["jobs"],
    });
  });

  it("selects an older feature version as rollback", () => {
    saveFeatureCatalogVersion(featureVersion());
    saveFeatureCatalogVersion(featureVersion({ id: "features-two" }));
    selectFeatureCatalogVersion("features-one");

    expect(selectedFeatureCatalogVersionId()).toBe("features-one");
  });

  it("treats corrupt storage as empty", () => {
    localStorage.setItem("waypoint-feature-catalog-versions", "not-json");
    expect(readFeatureCatalogVersions()).toEqual([]);
  });
});
