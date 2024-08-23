import { Button, Flex } from '@invoke-ai/ui-library';
import { createMemoizedSelector } from 'app/store/createMemoizedSelector';
import { useAppDispatch, useAppSelector } from 'app/store/storeHooks';
import { useDefaultIPAdapter } from 'features/controlLayers/hooks/useLayerControlAdapter';
import { nanoid } from 'features/controlLayers/konva/util';
import {
  rgIPAdapterAdded,
  rgNegativePromptChanged,
  rgPositivePromptChanged,
  selectCanvasV2Slice,
} from 'features/controlLayers/store/canvasV2Slice';
import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { PiPlusBold } from 'react-icons/pi';

type AddPromptButtonProps = {
  id: string;
};

export const RegionalGuidanceAddPromptsIPAdapterButtons = ({ id }: AddPromptButtonProps) => {
  const { t } = useTranslation();
  const dispatch = useAppDispatch();
  const defaultIPAdapter = useDefaultIPAdapter();
  const selectValidActions = useMemo(
    () =>
      createMemoizedSelector(selectCanvasV2Slice, (canvasV2) => {
        const rg = canvasV2.regions.entities.find((rg) => rg.id === id);
        return {
          canAddPositivePrompt: rg?.positivePrompt === null,
          canAddNegativePrompt: rg?.negativePrompt === null,
        };
      }),
    [id]
  );
  const validActions = useAppSelector(selectValidActions);
  const addPositivePrompt = useCallback(() => {
    dispatch(rgPositivePromptChanged({ id, prompt: '' }));
  }, [dispatch, id]);
  const addNegativePrompt = useCallback(() => {
    dispatch(rgNegativePromptChanged({ id, prompt: '' }));
  }, [dispatch, id]);
  const addIPAdapter = useCallback(() => {
    dispatch(rgIPAdapterAdded({ id, ipAdapter: { ...defaultIPAdapter, id: nanoid() } }));
  }, [defaultIPAdapter, dispatch, id]);

  return (
    <Flex w="full" p={2} justifyContent="space-between">
      <Button
        size="sm"
        variant="ghost"
        leftIcon={<PiPlusBold />}
        onClick={addPositivePrompt}
        isDisabled={!validActions.canAddPositivePrompt}
      >
        {t('common.positivePrompt')}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        leftIcon={<PiPlusBold />}
        onClick={addNegativePrompt}
        isDisabled={!validActions.canAddNegativePrompt}
      >
        {t('common.negativePrompt')}
      </Button>
      <Button size="sm" variant="ghost" leftIcon={<PiPlusBold />} onClick={addIPAdapter}>
        {t('common.ipAdapter')}
      </Button>
    </Flex>
  );
};