"use client";

import { useState } from "react";
import { getCallPhone } from "@/lib/api";

export function CallPhone({ winnerId }: { winnerId: string }) {
  const [phone, setPhone] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  async function reveal() {
    setLoading(true);
    try {
      setPhone((await getCallPhone(winnerId)).phone);
    } catch {
      setUnavailable(true);
    } finally {
      setLoading(false);
    }
  }

  if (phone) {
    return <span>Phone: <a href={`tel:+1${phone}`}>({phone.slice(0, 3)}) {phone.slice(3, 6)}-{phone.slice(6)}</a></span>;
  }
  if (unavailable) return <span>Phone unavailable</span>;
  return <button type="button" onClick={reveal} disabled={loading}>{loading ? "Looking up phone…" : "Show phone number"}</button>;
}
