import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export default function App() {
  return (
    <TooltipProvider>
      <div className="flex min-h-screen items-center justify-center gap-6 p-8">
        {/* Glass stat card */}
        <Card className="glass w-72">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Status
            </CardTitle>
            <span className="status-dot-green" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">Online</div>
            <p className="text-xs text-muted-foreground">Uptime: 2d 14h</p>
          </CardContent>
        </Card>

        {/* Primary accent card */}
        <Card className="glass w-72">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              LLM Cost
            </CardTitle>
            <Badge variant="secondary">today</Badge>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">$0.12</div>
            <p className="text-xs text-muted-foreground">47 calls · 12k tokens</p>
          </CardContent>
        </Card>

        {/* Action demo */}
        <Card className="glass w-72">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Design System
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Button className="w-full">Primary Action</Button>
            <Button variant="secondary" className="w-full">
              Secondary
            </Button>
            <Button variant="outline" className="w-full">
              Outline
            </Button>
            <div className="flex gap-2">
              <Badge className="bg-success text-white">Green</Badge>
              <Badge className="bg-warning text-white">Amber</Badge>
              <Badge className="bg-destructive text-white">Red</Badge>
              <Badge className="bg-info text-white">Blue</Badge>
            </div>
          </CardContent>
        </Card>
      </div>
      <Toaster />
    </TooltipProvider>
  );
}
