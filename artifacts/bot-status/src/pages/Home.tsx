import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { 
  Copy, 
  CheckCircle2, 
  Wallet, 
  ArrowRight,
  ShieldCheck,
  RefreshCw,
  QrCode,
  Zap,
  ChevronRight,
  XCircle,
  AlertCircle
} from 'lucide-react';
import { 
  useGetPaymentInfo, 
  useGetUpiBalance,
  useVerifyUtr,
  useCheckAdmin,
  getGetUpiBalanceQueryKey,
  getCheckAdminQueryKey
} from '@workspace/api-client-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useToast } from '@/hooks/use-toast';
import { Card } from '@/components/ui/card';
import AdminPanel from '@/components/AdminPanel';

// Fallback for getting Telegram user ID
const getTelegramUserId = (): number | null => {
  try {
    const initDataUnsafe = (window as any).Telegram?.WebApp?.initDataUnsafe;
    if (initDataUnsafe?.user?.id) {
      return Number(initDataUnsafe.user.id);
    }
  } catch (e) {
    // Ignore error
  }
  return null;
};

// Use the WebApp theme if available to make the app background match Telegram
const applyTelegramTheme = () => {
  try {
    if ((window as any).Telegram?.WebApp) {
      const webApp = (window as any).Telegram.WebApp;
      webApp.ready();
      webApp.expand(); // Expand to full height
      
      if (webApp.colorScheme === 'dark') {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    }
  } catch (e) {}
};

type ViewState = 'deposit' | 'verifying' | 'success' | 'error';

export default function Home() {
  const [telegramUserId] = useState<number | null>(getTelegramUserId());
  const [utr, setUtr] = useState('');
  const [viewState, setViewState] = useState<ViewState>('deposit');
  const [errorMsg, setErrorMsg] = useState('');
  const [successData, setSuccessData] = useState<{ amount: number, newBalance: number, ref: string } | null>(null);
  
  const { toast } = useToast();

  useEffect(() => {
    applyTelegramTheme();
  }, []);

  // Queries
  const { data: paymentInfo, isLoading: isPaymentInfoLoading } = useGetPaymentInfo();
  const { data: balanceInfo, refetch: refetchBalance } = useGetUpiBalance(telegramUserId ?? 0, {
    query: { enabled: !!telegramUserId && viewState === 'deposit', queryKey: getGetUpiBalanceQueryKey(telegramUserId ?? 0) }
  });
  // Automatically detects whether the logged-in Telegram user is the bot
  // admin (verified server-side from signed initData) so the admin panel can
  // appear without any manual login step.
  const { data: adminCheck } = useCheckAdmin({
    query: { enabled: !!telegramUserId, queryKey: getCheckAdminQueryKey() }
  });
  const isAdmin = !!adminCheck?.isAdmin;

  // Mutation
  const verifyMutation = useVerifyUtr();

  const handleCopyUpiId = () => {
    if (paymentInfo?.upiId) {
      navigator.clipboard.writeText(paymentInfo.upiId);
      toast({
        title: "UPI ID Copied",
        description: "You can now paste it in your UPI app.",
      });
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!telegramUserId) {
      toast({
        title: "Missing User ID",
        description: "Please enter your Telegram User ID for testing.",
        variant: "destructive"
      });
      return;
    }
    
    if (utr.trim().length < 6) {
      toast({
        title: "Invalid UTR",
        description: "Please enter a valid UTR / Transaction ID (usually 12 digits).",
        variant: "destructive"
      });
      return;
    }

    setViewState('verifying');
    
    verifyMutation.mutate({
      data: {
        telegramUserId,
        utr: utr.trim()
      }
    }, {
      onSuccess: (data) => {
        if (data.success) {
          setSuccessData({
            amount: data.amount,
            newBalance: data.newBalance,
            ref: data.ref
          });
          setViewState('success');
          refetchBalance(); // update balance in background
        } else {
          setErrorMsg("Could not verify this transaction. It may be invalid or already claimed.");
          setViewState('error');
        }
      },
      onError: (err: any) => {
        const message = err?.response?.data?.error || err?.message || "Failed to verify payment.";
        setErrorMsg(message);
        setViewState('error');
      }
    });
  };

  const resetForm = () => {
    setUtr('');
    setViewState('deposit');
    setSuccessData(null);
    setErrorMsg('');
  };

  // If no Telegram User ID, show fallback input (for testing in browser)
  const isMissingUserId = telegramUserId === null;

  return (
    <div className="min-h-[100dvh] w-full bg-background flex flex-col relative overflow-hidden pb-10">
      
      {/* Dynamic Header */}
      <header className="px-6 py-5 flex items-center justify-between sticky top-0 bg-background/80 backdrop-blur-md z-10 border-b border-border/50">
        <div className="flex items-center gap-2">
          <div className="h-10 w-10 bg-primary/10 rounded-full flex items-center justify-center text-primary">
            <Wallet className="h-5 w-5" />
          </div>
          <div>
            <h1 className="font-bold text-lg leading-tight text-foreground">Add Funds</h1>
            <p className="text-xs text-muted-foreground font-medium">Instant Deposit</p>
          </div>
        </div>
        
        {/* Balance Badge */}
        {telegramUserId && (
          <div className="flex flex-col items-end">
            <span className="text-xs text-muted-foreground font-medium mb-0.5">Current Balance</span>
            <div className="bg-white dark:bg-card border shadow-sm px-3 py-1.5 rounded-full flex items-center gap-1.5">
              <span className="font-bold text-sm text-foreground">${balanceInfo?.balance?.toFixed(2) ?? '---'}</span>
            </div>
          </div>
        )}
      </header>

      <main className="flex-1 flex flex-col max-w-md mx-auto w-full px-5 pt-6">
        
        <AnimatePresence mode="wait">
          
          {/* STATE: DEPOSIT FORM */}
          {viewState === 'deposit' && (
            <motion.div
              key="deposit"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              transition={{ duration: 0.3 }}
              className="flex flex-col gap-6"
            >
              
              {isAdmin && <AdminPanel paymentInfo={paymentInfo} />}

              {isMissingUserId && (
                <Card className="p-4 border-dashed border-2 bg-muted/20">
                  <h3 className="font-bold text-sm mb-2 text-foreground flex items-center gap-2">
                    <AlertCircle className="h-4 w-4 text-primary" />
                    Open from Telegram
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    We couldn't detect your Telegram account. Please open this page using the
                    "Add Funds" button inside the bot so your account is detected automatically.
                  </p>
                </Card>
              )}

              {/* QR Section */}
              <div className="bg-white dark:bg-card border rounded-3xl p-6 shadow-sm flex flex-col items-center text-center relative overflow-hidden">
                <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-primary via-purple-400 to-primary"></div>
                
                <h2 className="text-sm font-bold text-muted-foreground uppercase tracking-wider mb-5 flex items-center gap-2">
                  <QrCode className="h-4 w-4" />
                  Scan to Pay
                </h2>
                
                {isPaymentInfoLoading ? (
                  <div className="w-56 h-56 bg-muted/20 animate-pulse rounded-2xl mb-4"></div>
                ) : (
                  <div className="qr-container bg-white p-2 rounded-2xl mb-6 relative">
                    <img 
                      src={paymentInfo?.qrImageUrl || 'https://api.dicebear.com/7.x/shapes/svg?seed=fallbackQR'} 
                      alt="UPI QR Code" 
                      className="w-52 h-52 object-cover rounded-xl"
                    />
                    {/* Corner accents */}
                    <div className="absolute top-0 left-0 w-4 h-4 border-t-2 border-l-2 border-primary rounded-tl-xl" />
                    <div className="absolute top-0 right-0 w-4 h-4 border-t-2 border-r-2 border-primary rounded-tr-xl" />
                    <div className="absolute bottom-0 left-0 w-4 h-4 border-b-2 border-l-2 border-primary rounded-bl-xl" />
                    <div className="absolute bottom-0 right-0 w-4 h-4 border-b-2 border-r-2 border-primary rounded-br-xl" />
                  </div>
                )}

                <p className="text-sm text-muted-foreground mb-2">Or pay to this UPI ID:</p>
                <button 
                  onClick={handleCopyUpiId}
                  className="flex items-center gap-3 bg-secondary/50 hover:bg-secondary px-4 py-2.5 rounded-xl transition-colors active:scale-95 group"
                  data-testid="button-copy-upi"
                >
                  <span className="font-semibold text-foreground tracking-wide">
                    {isPaymentInfoLoading ? 'Loading...' : paymentInfo?.upiId}
                  </span>
                  <div className="bg-white shadow-sm p-1.5 rounded-md group-hover:text-primary transition-colors">
                    <Copy className="h-3.5 w-3.5" />
                  </div>
                </button>

                {paymentInfo?.minDeposit ? (
                  <div className="mt-5 px-3 py-1.5 bg-primary/5 text-primary text-xs font-semibold rounded-full">
                    Minimum deposit: ₹{paymentInfo.minDeposit}
                  </div>
                ) : null}
              </div>

              {/* UTR Input Section */}
              <form onSubmit={handleSubmit} className="flex flex-col gap-4">
                <div className="space-y-2">
                  <label htmlFor="utr" className="text-sm font-bold text-foreground pl-1">
                    Enter Reference Number
                  </label>
                  <div className="relative">
                    <Input
                      id="utr"
                      type="text"
                      placeholder="e.g. 312345678901"
                      value={utr}
                      onChange={(e) => setUtr(e.target.value)}
                      className="utr-input h-16 text-lg pl-5 pr-12 shadow-sm"
                      data-testid="input-utr"
                    />
                    <div className="absolute right-4 top-1/2 -translate-y-1/2 text-muted-foreground">
                      <ShieldCheck className="h-5 w-5" />
                    </div>
                  </div>
                  <p className="text-[11px] text-muted-foreground pl-1 font-medium">
                    Enter the 12-digit UTR / Transaction ID from your UPI app.
                  </p>
                </div>

                <Button 
                  type="submit" 
                  size="lg" 
                  className="w-full text-base group mt-2"
                  disabled={!utr || isMissingUserId || isPaymentInfoLoading}
                  data-testid="button-submit-utr"
                >
                  Verify & Add Balance
                  <ArrowRight className="ml-2 h-4 w-4 group-hover:translate-x-1 transition-transform" />
                </Button>
              </form>

              {/* How it works */}
              <div className="mt-4 pt-6 border-t flex items-center justify-between px-2 text-center text-xs font-medium text-muted-foreground">
                <div className="flex flex-col items-center gap-1.5 w-20">
                  <div className="h-8 w-8 rounded-full bg-secondary flex items-center justify-center text-foreground font-bold">1</div>
                  <span>Scan & Pay</span>
                </div>
                <div className="h-px bg-border flex-1 mx-2 mt-[-16px]"></div>
                <div className="flex flex-col items-center gap-1.5 w-20">
                  <div className="h-8 w-8 rounded-full bg-secondary flex items-center justify-center text-foreground font-bold">2</div>
                  <span>Copy UTR</span>
                </div>
                <div className="h-px bg-border flex-1 mx-2 mt-[-16px]"></div>
                <div className="flex flex-col items-center gap-1.5 w-20">
                  <div className="h-8 w-8 rounded-full bg-secondary flex items-center justify-center text-foreground font-bold">3</div>
                  <span>Get Balance</span>
                </div>
              </div>

            </motion.div>
          )}

          {/* STATE: VERIFYING */}
          {viewState === 'verifying' && (
            <motion.div
              key="verifying"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              className="flex flex-col items-center justify-center py-20 text-center"
            >
              <div className="relative mb-8">
                <div className="absolute inset-0 bg-primary/20 rounded-full blur-xl animate-pulse"></div>
                <div className="h-24 w-24 bg-white shadow-xl rounded-full flex items-center justify-center relative z-10 border-4 border-primary/10">
                  <RefreshCw className="h-10 w-10 text-primary animate-spin" />
                </div>
              </div>
              <h2 className="text-2xl font-bold text-foreground mb-2">Verifying Payment</h2>
              <p className="text-muted-foreground max-w-[250px]">
                Checking transaction <span className="font-mono text-foreground font-medium">{utr}</span>. This usually takes a few seconds.
              </p>
            </motion.div>
          )}

          {/* STATE: SUCCESS */}
          {viewState === 'success' && successData && (
            <motion.div
              key="success"
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              className="flex flex-col items-center justify-center py-10"
            >
              <div className="mb-6 relative">
                <motion.div 
                  initial={{ scale: 0 }}
                  animate={{ scale: 1 }}
                  transition={{ type: "spring", stiffness: 200, damping: 15 }}
                  className="h-28 w-28 bg-success rounded-full flex items-center justify-center shadow-lg shadow-success/30 z-10 relative"
                >
                  <CheckCircle2 className="h-14 w-14 text-white" />
                </motion.div>
                {/* Decorative particles */}
                <motion.div 
                  initial={{ opacity: 0, scale: 0 }}
                  animate={{ opacity: 1, scale: 1.5 }}
                  transition={{ delay: 0.2, duration: 0.5 }}
                  className="absolute inset-0 border-2 border-success rounded-full -z-10"
                ></motion.div>
              </div>
              
              <h2 className="text-3xl font-extrabold text-foreground mb-2 tracking-tight">Payment Successful</h2>
              <p className="text-muted-foreground mb-8 text-center max-w-[280px]">
                Your funds have been added and are ready to use.
              </p>

              <Card className="w-full bg-white dark:bg-card shadow-sm border overflow-hidden mb-8">
                <div className="p-6 flex flex-col gap-5">
                  <div className="flex justify-between items-center pb-5 border-b border-dashed">
                    <span className="text-muted-foreground font-medium text-sm">Amount Added</span>
                    <span className="text-2xl font-bold text-success">₹{successData.amount}</span>
                  </div>
                  
                  <div className="flex justify-between items-center">
                    <span className="text-muted-foreground font-medium text-sm">New Balance</span>
                    <span className="text-lg font-bold text-foreground">${successData.newBalance.toFixed(2)}</span>
                  </div>
                  
                  <div className="flex justify-between items-center">
                    <span className="text-muted-foreground font-medium text-sm">Reference</span>
                    <span className="text-sm font-mono text-foreground font-medium">{successData.ref}</span>
                  </div>
                </div>
              </Card>

              <Button 
                onClick={resetForm} 
                size="lg" 
                className="w-full"
              >
                Make Another Deposit
              </Button>
            </motion.div>
          )}

          {/* STATE: ERROR */}
          {viewState === 'error' && (
            <motion.div
              key="error"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex flex-col items-center justify-center py-10"
            >
              <div className="h-24 w-24 bg-destructive/10 rounded-full flex items-center justify-center mb-6">
                <XCircle className="h-12 w-12 text-destructive" />
              </div>
              
              <h2 className="text-2xl font-bold text-foreground mb-3 text-center">Verification Failed</h2>
              
              <div className="bg-destructive/5 border border-destructive/20 rounded-2xl p-5 mb-8 text-center max-w-sm w-full">
                <p className="text-sm font-medium text-destructive mb-1">Error Details</p>
                <p className="text-foreground text-sm font-medium">{errorMsg}</p>
                <p className="text-xs text-muted-foreground mt-3 pt-3 border-t border-destructive/10">
                  Transaction ID: <span className="font-mono">{utr}</span>
                </p>
              </div>

              <div className="w-full flex flex-col gap-3">
                <Button 
                  onClick={() => setViewState('deposit')} 
                  size="lg" 
                  className="w-full"
                >
                  Edit UTR & Try Again
                </Button>
                <Button 
                  onClick={resetForm} 
                  variant="outline" 
                  size="lg" 
                  className="w-full"
                >
                  Start Over
                </Button>
              </div>
            </motion.div>
          )}

        </AnimatePresence>
      </main>
    </div>
  );
}
