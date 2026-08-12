export interface Point {
  x: number;
  y: number;
}

export interface PixelSize {
  width: number;
  height: number;
}

export interface ScreenArea {
  position: Point;
  size: PixelSize;
}

export interface ScreenMonitor extends ScreenArea {
  scaleFactor: number;
  name?: string | null;
}

export interface SavedWindowRect extends Point, PixelSize {
  monitor?: string | null;
}

export interface MonitorPlacement extends Point {
  monitor?: string | null;
}

function sameArea(left: ScreenArea, right: ScreenArea): boolean {
  return (
    left.position.x === right.position.x &&
    left.position.y === right.position.y &&
    left.size.width === right.size.width &&
    left.size.height === right.size.height
  );
}

export function preferredSecondaryPosition(
  monitors: ScreenMonitor[],
  primary: ScreenArea | null,
  windowSize: PixelSize,
): Point | null {
  if (monitors.length < 2) return null;
  const secondary = primary
    ? monitors.find((monitor) => !sameArea(monitor, primary))
    : monitors[1];
  if (!secondary) return null;

  const margin = Math.max(8, Math.round(16 * Math.max(secondary.scaleFactor, 1)));
  return {
    x: Math.max(
      secondary.position.x + margin,
      secondary.position.x + secondary.size.width - windowSize.width - margin,
    ),
    y: secondary.position.y + margin,
  };
}

function logicalWorkArea(monitor: ScreenMonitor): number {
  const scaleFactor = Math.max(monitor.scaleFactor, 1);
  return (monitor.size.width * monitor.size.height) / (scaleFactor * scaleFactor);
}

export function preferredSmallMonitorPlacement(
  monitors: ScreenMonitor[],
  windowSize: PixelSize,
): MonitorPlacement | null {
  const target = monitors.reduce<ScreenMonitor | null>((smallest, monitor) => {
    if (!smallest) return monitor;
    return logicalWorkArea(monitor) < logicalWorkArea(smallest) ? monitor : smallest;
  }, null);
  if (!target) return null;

  const margin = Math.max(8, Math.round(16 * Math.max(target.scaleFactor, 1)));
  return {
    x: Math.max(
      target.position.x + margin,
      target.position.x + target.size.width - windowSize.width - margin,
    ),
    y: target.position.y + margin,
    monitor: target.name ?? null,
  };
}

export function restorableWindowRect(
  saved: SavedWindowRect | null,
  monitors: ScreenMonitor[],
): SavedWindowRect | null {
  if (!saved || saved.width < 390 || saved.height < 620 || monitors.length === 0) return null;
  const intersects = monitors.some((monitor) => {
    const visibleWidth = Math.min(saved.x + saved.width, monitor.position.x + monitor.size.width)
      - Math.max(saved.x, monitor.position.x);
    const visibleHeight = Math.min(saved.y + saved.height, monitor.position.y + monitor.size.height)
      - Math.max(saved.y, monitor.position.y);
    return visibleWidth >= 80 && visibleHeight >= 48;
  });
  return intersects ? saved : null;
}
