"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { LoginForm } from "@/components/LoginForm";
import { RunStart } from "@/components/RunStart";

export default function Home() {
  const [authed, setAuthed] = useState(false);
  const router = useRouter();

  return (
    <main>
      {!authed ? (
        <LoginForm onSuccess={() => setAuthed(true)} />
      ) : (
        <>
          <p>
            <Link href="/calls">Calls to place →</Link>
          </p>
          <RunStart onStarted={(run) => router.push(`/runs/${run.id}`)} />
        </>
      )}
    </main>
  );
}
