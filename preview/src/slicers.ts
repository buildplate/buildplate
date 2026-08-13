export type SlicerInfo = {
  id: string;
  name: string;
  installed: boolean;
};

export async function fetchSlicers(): Promise<SlicerInfo[]> {
  const res = await fetch("/slicers");
  if (!res.ok) return [];
  const data = (await res.json()) as { slicers?: SlicerInfo[] };
  return data.slicers || [];
}

export async function openInSlicer(slicer: string, src: string, stl: Blob): Promise<void> {
  const q = new URLSearchParams({ slicer, src });
  const res = await fetch(`/slicers/open?${q}`, {
    method: "POST",
    headers: { "Content-Type": "model/stl" },
    body: stl,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.error || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
}
