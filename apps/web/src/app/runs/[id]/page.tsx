"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { HandoffReceipt } from "@/components/HandoffReceipt";
import { LoopProgress } from "@/components/LoopProgress";
import { RetryPanel } from "@/components/RetryPanel";
import { RunStatus } from "@/components/RunStatus";
import { WinnerReview } from "@/components/WinnerReview";
import {
  ApiError,
  TERMINAL_STATES,
  createHandoff,
  getRun,
  killRun,
  type RunDetail,
} from "@/lib/api";

const POLL_MS = 2000;

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [handingOff, setHandingOff] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRun(await getRun(id));
      setConnectionError(null);
    } catch (e) {
      // Truth before polish: a fetch failure is a visible reconnecting state,
      // never a silently stale screen.
      setConnectionError(
        e instanceof ApiError && e.status === 401
          ? "Session expired. Return to the start page and sign in again."
          : "Cannot reach the API. Retrying…",
      );
    }
  }, [id]);

  // Gate on a primitive: with `run` in the deps, every poll's fresh object
  // identity re-armed the effect and its setTimeout(0), so the page refetched
  // at network speed instead of every POLL_MS.
  const done =
    run !== null && TERMINAL_STATES.has(run.status) && run.handoffs.length > 0;

  useEffect(() => {
    if (done) return;
    const kickoff = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      window.clearTimeout(kickoff);
      window.clearInterval(timer);
    };
  }, [done, refresh]);

  async function onKill() {
    setActionError(null);
    try {
      await killRun(id);
      await refresh();
    } catch (e) {
      setActionError(`Kill failed: ${e instanceof Error ? e.message : e}`);
    }
  }

  async function onHandoff() {
    setActionError(null);
    setHandingOff(true);
    try {
      await createHandoff(id);
      await refresh();
    } catch (e) {
      setActionError(`Handoff failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setHandingOff(false);
    }
  }

  return (
    <main>
      <p>
        <Link href="/">← Start page</Link>
      </p>
      {connectionError && <p role="alert" className="error">{connectionError}</p>}
      {actionError && <p role="alert" className="error">{actionError}</p>}
      {run === null && !connectionError && <p role="status">Loading run…</p>}
      {run && (
        <>
          <RetryPanel run={run} />
          <RunStatus run={run} onKill={onKill} />
          <LoopProgress run={run} />
          <WinnerReview run={run} onHandoff={onHandoff} handingOff={handingOff} />
          <HandoffReceipt handoffs={run.handoffs} />
        </>
      )}
    </main>
  );
}
