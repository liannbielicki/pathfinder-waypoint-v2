"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export function AppNav() {
  const pathname = usePathname();
  const workbench = pathname.startsWith("/context-workbench");
  return <nav className="app-nav" aria-label="Primary">
    <Link href="/" aria-current={workbench ? undefined : "page"}>Waypoint</Link>
    <Link href="/context-workbench" aria-current={workbench ? "page" : undefined}>Context Workbench</Link>
  </nav>;
}
