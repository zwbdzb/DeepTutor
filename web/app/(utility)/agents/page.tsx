import AgentsHub from "@/components/agents/AgentsHub";
import { FeaturePage } from "@/components/layout/FeaturePage";
import { BackToHub } from "@/components/space/SpaceMain";

export default function AgentsPage() {
  return (
    <FeaturePage>
      <BackToHub pathname="/agents" />
      <AgentsHub />
    </FeaturePage>
  );
}
