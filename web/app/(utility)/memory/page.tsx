import MemoryHub from "@/components/memory/MemoryHub";
import { FeaturePage } from "@/components/layout/FeaturePage";
import { BackToHub } from "@/components/space/SpaceMain";

export default function MemoryPage() {
  return (
    <FeaturePage>
      <BackToHub pathname="/memory" />
      <MemoryHub />
    </FeaturePage>
  );
}
