import type { Metadata } from "next";
import { AppNav } from "@/components/AppNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pathfinder Waypoint",
  description: "Operator console for the Pathfinder retention loop",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body><AppNav />{children}</body>
    </html>
  );
}
