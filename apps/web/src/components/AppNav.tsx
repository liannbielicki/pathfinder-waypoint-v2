"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { isUnauthorized } from "@/lib/api";
import { getWorkbenchStatus } from "@/lib/workbench";

export function AppNav() {
  const pathname = usePathname();
  const workbench = pathname.startsWith("/context-workbench");
  const [workbenchLocked, setWorkbenchLocked] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const refresh = async () => {
      try {
        const status = await getWorkbenchStatus();
        if (!cancelled) setWorkbenchLocked(status.activity === "waypoint");
      } catch (cause) {
        // Signed-out navigation remains available; the destination owns login.
        // Signing back in remounts this nav, so stopping here costs nothing.
        if (isUnauthorized(cause)) {
          cancelled = true;
          window.clearInterval(timer);
        }
      }
    };
    void refresh();
    timer = window.setInterval(() => void refresh(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
  return <nav className="app-nav" aria-label="Primary">
    <Link href="/" aria-current={workbench ? undefined : "page"}>Waypoint</Link>
    {workbenchLocked && !workbench
      ? <span aria-disabled="true">Context Workbench (locked)</span>
      : <Link href="/context-workbench" aria-current={workbench ? "page" : undefined}>Context Workbench</Link>}
  </nav>;
}
