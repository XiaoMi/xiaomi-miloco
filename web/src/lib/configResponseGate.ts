export function createConfigResponseGate() {
  let revision = 0;
  return {
    beginRead: () => ++revision,
    isCurrent: (ticket: number) => ticket === revision,
    invalidate: () => { revision++; },
  };
}
