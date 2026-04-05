import { motion, useReducedMotion } from "framer-motion";
import type { ReactNode } from "react";

interface PageTransitionProps {
  children: ReactNode;
}

const variants = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
};

const reducedVariants = {
  initial: { opacity: 1 },
  animate: { opacity: 1 },
  exit: { opacity: 1 },
};

export function PageTransition({ children }: PageTransitionProps) {
  const prefersReducedMotion = useReducedMotion();

  return (
    <motion.div
      variants={prefersReducedMotion ? reducedVariants : variants}
      initial="initial"
      animate="animate"
      exit="exit"
      transition={
        prefersReducedMotion
          ? { duration: 0 }
          : { duration: 0.2, ease: "easeOut" }
      }
    >
      {children}
    </motion.div>
  );
}
