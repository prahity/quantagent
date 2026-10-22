import { RunView } from "@/components/run-view";

export default async function RunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <RunView runId={runId} />;
}
