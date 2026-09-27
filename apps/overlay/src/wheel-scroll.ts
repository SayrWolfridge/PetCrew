export interface ScrollTarget {
  clientHeight: number;
  scrollHeight: number;
  scrollTop: number;
}

const LINE_HEIGHT_PX = 16;

export function wheelDeltaInPixels(
  deltaY: number,
  deltaMode: number,
  clientHeight: number,
): number {
  if (!Number.isFinite(deltaY)) return 0;
  if (deltaMode === 1) return deltaY * LINE_HEIGHT_PX;
  if (deltaMode === 2) return deltaY * clientHeight;
  return deltaY;
}

export function scrollTargetFromWheel(
  target: ScrollTarget,
  deltaY: number,
  deltaMode = 0,
): boolean {
  const maximum = Math.max(0, target.scrollHeight - target.clientHeight);
  const delta = wheelDeltaInPixels(deltaY, deltaMode, target.clientHeight);
  const next = Math.min(maximum, Math.max(0, target.scrollTop + delta));
  if (next === target.scrollTop) return false;
  target.scrollTop = next;
  return true;
}
