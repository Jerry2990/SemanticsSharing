#!/usr/bin/env python
import torch
import getopt
import math
import numpy
import os
import PIL
import PIL.Image
import sys
import cv2
import light.map.flow_vis as flow_vis
import light.map.correlation as correlation
try:
    from correlation import correlation  # the custom cost volume layer
except:
    sys.path.insert(0, './correlation');
    import light.map.correlation  # you should consider upgrading python
# end

##########################################################

#assert (int(str('').join(torch.__version__.split('.')[0:3])) >= 41)  # requires at least pytorch version 0.4.1

torch.set_grad_enabled(False)  # make sure to not compute gradients for computational performance

torch.backends.cudnn.enabled = False  # make sure to use cudnn for computational performance

##########################################################

arguments_strModel = 'kitti'
arguments_strFirst = './images/120_60r.jpg'
arguments_strSecond = './images/60r.jpg'
arguments_strOut = './out.flo'

for strOption, strArgument in \
getopt.getopt(sys.argv[1:], '', [strParameter[2:] + '=' for strParameter in sys.argv[1::2]])[0]:
    if strOption == '--model' and strArgument != '': arguments_strModel = strArgument  # which model to use
    if strOption == '--first' and strArgument != '': arguments_strFirst = strArgument  # path to the first frame
    if strOption == '--second' and strArgument != '': arguments_strSecond = strArgument  # path to the second frame
    if strOption == '--out' and strArgument != '': arguments_strOut = strArgument  # path to where the output should be stored
# end

##########################################################

Backward_tensorGrid = {}


def Backward(tensorInput, tensorFlow):
    if str(tensorFlow.size()) not in Backward_tensorGrid:
        tensorHorizontal = torch.linspace(-1.0, 1.0, tensorFlow.size(3)).view(1, 1, 1, tensorFlow.size(3)).expand(
            tensorFlow.size(0), -1, tensorFlow.size(2), -1)
        tensorVertical = torch.linspace(-1.0, 1.0, tensorFlow.size(2)).view(1, 1, tensorFlow.size(2), 1).expand(
            tensorFlow.size(0), -1, -1, tensorFlow.size(3))

        Backward_tensorGrid[str(tensorFlow.size())] = torch.cat([tensorHorizontal, tensorVertical], 1).cuda()
    # end

    tensorFlow = torch.cat([tensorFlow[:, 0:1, :, :] / ((tensorInput.size(3) - 1.0) / 2.0),
                            tensorFlow[:, 1:2, :, :] / ((tensorInput.size(2) - 1.0) / 2.0)], 
                           1)

    return torch.nn.functional.grid_sample(input=tensorInput,
                                           grid=(Backward_tensorGrid[str(tensorFlow.size())] + tensorFlow).permute(0, 2, 3,1),
                                           mode='bilinear', padding_mode='zeros')

# end

##########################################################

class Network_single(torch.nn.Module):
    def __init__(self):
        super(Network_single, self).__init__()

        class Features(torch.nn.Module):
            def __init__(self):
                super(Features, self).__init__()

                self.moduleOne = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=3, out_channels=32, kernel_size=7, stride=1, padding=3),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleTwo = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleThr = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleFou = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=64, out_channels=96, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=96, out_channels=96, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleFiv = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=96, out_channels=128, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleSix = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=128, out_channels=192, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

            # end

            def forward(self, tensorInput):
                tensorOne = self.moduleOne(tensorInput)
                tensorTwo = self.moduleTwo(tensorOne)
                tensorThr = self.moduleThr(tensorTwo)
                tensorFou = self.moduleFou(tensorThr)
                tensorFiv = self.moduleFiv(tensorFou)
                tensorSix = self.moduleSix(tensorFiv)

                return [tensorOne, tensorTwo, tensorThr, tensorFou, tensorFiv, tensorSix]

        # end
        # end

        class Matching(torch.nn.Module):
            def __init__(self, intLevel):
                super(Matching, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                if intLevel != 2:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel == 2:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                if intLevel == 6:
                    self.moduleUpflow = None

                elif intLevel != 6:
                    self.moduleUpflow = torch.nn.ConvTranspose2d(in_channels=2, out_channels=2, kernel_size=4, stride=2,
                                                                 padding=1, bias=False, groups=2)

                # end

                if intLevel >= 4:
                    self.moduleUpcorr = None

                elif intLevel < 4:
                    self.moduleUpcorr = torch.nn.ConvTranspose2d(in_channels=49, out_channels=49, kernel_size=4,
                                                                 stride=2, padding=1, bias=False, groups=49)

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=49, out_channels=128, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel],
                                    stride=1, padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                )

            # end

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):
                tensorFeaturesFirst = self.moduleFeat(tensorFeaturesFirst)
                tensorFeaturesSecond = self.moduleFeat(tensorFeaturesSecond)

                if tensorFlow is not None:
                    tensorFlow = self.moduleUpflow(tensorFlow)
                # end

                if tensorFlow is not None:
                    tensorFeaturesSecond = Backward(tensorInput=tensorFeaturesSecond,
                                                    tensorFlow=tensorFlow * self.dblBackward)
                # end

                if self.moduleUpcorr is None:
                    tensorCorrelation = torch.nn.functional.leaky_relu(
                        input=correlation.FunctionCorrelation(tensorFirst=tensorFeaturesFirst,
                                                              tensorSecond=tensorFeaturesSecond, intStride=1),
                        negative_slope=0.1, inplace=False)

                elif self.moduleUpcorr is not None:
                    tensorCorrelation = self.moduleUpcorr(torch.nn.functional.leaky_relu(
                        input=correlation.FunctionCorrelation(tensorFirst=tensorFeaturesFirst,
                                                              tensorSecond=tensorFeaturesSecond, intStride=2),
                        negative_slope=0.1, inplace=False))

                # end

                return (tensorFlow if tensorFlow is not None else 0.0) + self.moduleMain(tensorCorrelation)

        # end
        # end

        class Subpixel(torch.nn.Module):
            def __init__(self, intLevel):
                super(Subpixel, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                if intLevel != 2:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel == 2:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=[0, 0, 130, 130, 194, 258, 386][intLevel], out_channels=128,
                                    kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel],
                                    stride=1, padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                )

            # end

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):
                tensorFeaturesFirst = self.moduleFeat(tensorFeaturesFirst)
                tensorFeaturesSecond = self.moduleFeat(tensorFeaturesSecond)

                if tensorFlow is not None:
                    tensorFeaturesSecond = Backward(tensorInput=tensorFeaturesSecond,
                                                    tensorFlow=tensorFlow * self.dblBackward)
                # end

                return (tensorFlow if tensorFlow is not None else 0.0) + self.moduleMain(
                    torch.cat([tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow], 1))

        # end
        # end

        class Regularization(torch.nn.Module):
            def __init__(self, intLevel):
                super(Regularization, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                self.intUnfold = [0, 0, 7, 5, 5, 3, 3][intLevel]

                if intLevel >= 5:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel < 5:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=[0, 0, 32, 64, 96, 128, 192][intLevel], out_channels=128,
                                        kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=[0, 0, 131, 131, 131, 131, 195][intLevel], out_channels=128,
                                    kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                if intLevel >= 5:
                    self.moduleDist = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel], stride=1,
                                        padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                    )

                elif intLevel < 5:
                    self.moduleDist = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=([0, 0, 7, 5, 5, 3, 3][intLevel], 1), stride=1,
                                        padding=([0, 0, 3, 2, 2, 1, 1][intLevel], 0)),
                        torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=(1, [0, 0, 7, 5, 5, 3, 3][intLevel]), stride=1,
                                        padding=(0, [0, 0, 3, 2, 2, 1, 1][intLevel]))
                    )

                # end

                self.moduleScaleX = torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel], out_channels=1,
                                                    kernel_size=1, stride=1, padding=0)
                self.moduleScaleY = torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel], out_channels=1,
                                                    kernel_size=1, stride=1, padding=0)

            # eny

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):
                tensorDifference = (
                        tensorFirst - Backward(tensorInput=tensorSecond, tensorFlow=tensorFlow * self.dblBackward)).pow(
                    2.0).sum(1, True).sqrt()

                tensorDist = self.moduleDist(self.moduleMain(torch.cat([tensorDifference,
                                                                        tensorFlow - tensorFlow.view(tensorFlow.size(0),
                                                                                                     2, -1).mean(2,
                                                                                                                 True).view(
                                                                            tensorFlow.size(0), 2, 1, 1),
                                                                        self.moduleFeat(tensorFeaturesFirst)], 1)))
                tensorDist = tensorDist.pow(2.0).neg()
                tensorDist = (tensorDist - tensorDist.max(1, True)[0]).exp()

                tensorDivisor = tensorDist.sum(1, True).reciprocal()

                tensorScaleX = self.moduleScaleX(
                    tensorDist * torch.nn.functional.unfold(input=tensorFlow[:, 0:1, :, :], kernel_size=self.intUnfold,
                                                            stride=1, padding=int((self.intUnfold - 1) / 2)).view_as(
                        tensorDist)) * tensorDivisor
                tensorScaleY = self.moduleScaleY(
                    tensorDist * torch.nn.functional.unfold(input=tensorFlow[:, 1:2, :, :], kernel_size=self.intUnfold,
                                                            stride=1, padding=int((self.intUnfold - 1) / 2)).view_as(
                        tensorDist)) * tensorDivisor

                return torch.cat([tensorScaleX, tensorScaleY], 1)

        # end
        # end

        self.moduleFeatures = Features()
        self.moduleMatching = torch.nn.ModuleList([Matching(intLevel) for intLevel in [2, 3, 4, 5, 6]])
        self.moduleSubpixel = torch.nn.ModuleList([Subpixel(intLevel) for intLevel in [2, 3, 4, 5, 6]])
        self.moduleRegularization = torch.nn.ModuleList([Regularization(intLevel) for intLevel in [2, 3, 4, 5, 6]])

        self.load_state_dict(torch.load('./network-' + arguments_strModel + '.pytorch'))

    # end

    def forward(self, tensorFirst, tensorSecond):
        tensorFirst[:, 0, :, :] = tensorFirst[:, 0, :, :] - 0.411618
        tensorFirst[:, 1, :, :] = tensorFirst[:, 1, :, :] - 0.434631
        tensorFirst[:, 2, :, :] = tensorFirst[:, 2, :, :] - 0.454253

        tensorSecond[:, 0, :, :] = tensorSecond[:, 0, :, :] - 0.410782
        tensorSecond[:, 1, :, :] = tensorSecond[:, 1, :, :] - 0.433645
        tensorSecond[:, 2, :, :] = tensorSecond[:, 2, :, :] - 0.452793

        tensorFeaturesFirst = self.moduleFeatures(tensorFirst)
        tensorFeaturesSecond = self.moduleFeatures(tensorSecond)

        tensorFirst = [tensorFirst]
        tensorSecond = [tensorSecond]

        for intLevel in [1, 2, 3, 4, 5]:
            tensorFirst.append(torch.nn.functional.interpolate(input=tensorFirst[-1], size=(
            tensorFeaturesFirst[intLevel].size(2), tensorFeaturesFirst[intLevel].size(3)), mode='bilinear',
                                                               align_corners=False))
            tensorSecond.append(torch.nn.functional.interpolate(input=tensorSecond[-1], size=(
            tensorFeaturesSecond[intLevel].size(2), tensorFeaturesSecond[intLevel].size(3)), mode='bilinear',
                                                                align_corners=False))
        # end

        tensorFlow = None

        for intLevel in [-1, -2, -3, -4, -5]:
            tensorFlow = self.moduleMatching[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                       tensorFeaturesFirst[intLevel], tensorFeaturesSecond[intLevel],
                                                       tensorFlow)
            tensorFlow = self.moduleSubpixel[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                       tensorFeaturesFirst[intLevel], tensorFeaturesSecond[intLevel],
                                                       tensorFlow)
            tensorFlow = self.moduleRegularization[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                             tensorFeaturesFirst[intLevel],
                                                             tensorFeaturesSecond[intLevel], tensorFlow)
        # end

        return tensorFlow * 20.0


# end
# end




class Network(torch.nn.Module):
    def __init__(self):
        super(Network, self).__init__()

        class Features(torch.nn.Module):
            def __init__(self):
                super(Features, self).__init__()

                self.moduleOne = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=3, out_channels=32, kernel_size=7, stride=1, padding=3),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleTwo = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleThr = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleFou = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=64, out_channels=96, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=96, out_channels=96, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleFiv = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=96, out_channels=128, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                self.moduleSix = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=128, out_channels=192, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

            # end

            def forward(self, tensorInput):
                tensorOne = self.moduleOne(tensorInput)
                tensorTwo = self.moduleTwo(tensorOne)
                tensorThr = self.moduleThr(tensorTwo)
                tensorFou = self.moduleFou(tensorThr)
                tensorFiv = self.moduleFiv(tensorFou)
                tensorSix = self.moduleSix(tensorFiv)

                return [tensorOne, tensorTwo, tensorThr, tensorFou, tensorFiv, tensorSix]

        # end
        # end

        class Matching(torch.nn.Module):
            def __init__(self, intLevel):
                super(Matching, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                if intLevel != 2:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel == 2:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                if intLevel == 6:
                    self.moduleUpflow = None

                elif intLevel != 6:
                    self.moduleUpflow = torch.nn.ConvTranspose2d(in_channels=2, out_channels=2, kernel_size=4, stride=2,
                                                                 padding=1, bias=False, groups=2)

                # end

                if intLevel >= 4:
                    self.moduleUpcorr = None

                elif intLevel < 4:
                    self.moduleUpcorr = torch.nn.ConvTranspose2d(in_channels=49, out_channels=49, kernel_size=4,
                                                                 stride=2, padding=1, bias=False, groups=49)

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=49, out_channels=128, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel],
                                    stride=1, padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                )

            # end

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):
                tensorFeaturesFirst = self.moduleFeat(tensorFeaturesFirst)
                tensorFeaturesSecond = self.moduleFeat(tensorFeaturesSecond)

                if tensorFlow is not None:
                    tensorFlow = self.moduleUpflow(tensorFlow)
                # end

                if tensorFlow is not None:
                    tensorFeaturesSecond = Backward(tensorInput=tensorFeaturesSecond,
                                                    tensorFlow=tensorFlow * self.dblBackward)
                # end

                if self.moduleUpcorr is None:
                    tensorCorrelation = torch.nn.functional.leaky_relu(
                        input=correlation.FunctionCorrelation(tensorFirst=tensorFeaturesFirst,
                                                              tensorSecond=tensorFeaturesSecond, intStride=1),
                        negative_slope=0.1, inplace=False)

                elif self.moduleUpcorr is not None:
                    tensorCorrelation = self.moduleUpcorr(torch.nn.functional.leaky_relu(
                        input=correlation.FunctionCorrelation(tensorFirst=tensorFeaturesFirst,
                                                              tensorSecond=tensorFeaturesSecond, intStride=2),
                        negative_slope=0.1, inplace=False))

                # end

                return (tensorFlow if tensorFlow is not None else 0.0) + self.moduleMain(tensorCorrelation)

        # end
        # end

        class Subpixel(torch.nn.Module):
            def __init__(self, intLevel):
                super(Subpixel, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                if intLevel != 2:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel == 2:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=[0, 0, 130, 130, 194, 258, 386][intLevel], out_channels=128,
                                    kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel],
                                    stride=1, padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                )

            # end

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):
                tensorFeaturesFirst = self.moduleFeat(tensorFeaturesFirst)
                tensorFeaturesSecond = self.moduleFeat(tensorFeaturesSecond)

                if tensorFlow is not None:
                    tensorFeaturesSecond = Backward(tensorInput=tensorFeaturesSecond,
                                                    tensorFlow=tensorFlow * self.dblBackward)
                # end

                return (tensorFlow if tensorFlow is not None else 0.0) + self.moduleMain(
                    torch.cat([tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow], 1))

        # end
        # end

        class Regularization(torch.nn.Module):
            def __init__(self, intLevel):
                super(Regularization, self).__init__()

                self.dblBackward = [0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625][intLevel]

                self.intUnfold = [0, 0, 7, 5, 5, 3, 3][intLevel]

                if intLevel >= 5:
                    self.moduleFeat = torch.nn.Sequential()

                elif intLevel < 5:
                    self.moduleFeat = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=[0, 0, 32, 64, 96, 128, 192][intLevel], out_channels=128,
                                        kernel_size=1, stride=1, padding=0),
                        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                    )

                # end

                self.moduleMain = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=[0, 0, 131, 131, 131, 131, 195][intLevel], out_channels=128,
                                    kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
                )

                if intLevel >= 5:
                    self.moduleDist = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=[0, 0, 7, 5, 5, 3, 3][intLevel], stride=1,
                                        padding=[0, 0, 3, 2, 2, 1, 1][intLevel])
                    )

                elif intLevel < 5:
                    self.moduleDist = torch.nn.Sequential(
                        torch.nn.Conv2d(in_channels=32, out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=([0, 0, 7, 5, 5, 3, 3][intLevel], 1), stride=1,
                                        padding=([0, 0, 3, 2, 2, 1, 1][intLevel], 0)),
                        torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        out_channels=[0, 0, 49, 25, 25, 9, 9][intLevel],
                                        kernel_size=(1, [0, 0, 7, 5, 5, 3, 3][intLevel]), stride=1,
                                        padding=(0, [0, 0, 3, 2, 2, 1, 1][intLevel]))
                    )

                # end

                self.moduleScaleX = torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel], out_channels=1,
                                                    kernel_size=1, stride=1, padding=0)
                self.moduleScaleY = torch.nn.Conv2d(in_channels=[0, 0, 49, 25, 25, 9, 9][intLevel], out_channels=1,
                                                    kernel_size=1, stride=1, padding=0)

            # eny

            def forward(self, tensorFirst, tensorSecond, tensorFeaturesFirst, tensorFeaturesSecond, tensorFlow):

                tensorDifference = (tensorFirst - Backward(tensorInput=tensorSecond, tensorFlow=tensorFlow * self.dblBackward)).pow(2.0).sum(1, True).sqrt()
                tensorDist = self.moduleDist(self.moduleMain(torch.cat([tensorDifference,
                                                                        tensorFlow - tensorFlow.view(tensorFlow.size(0), 2,-1).mean(2, True).view(tensorFlow.size(0), 2, 1, 1),
                                                                        self.moduleFeat(tensorFeaturesFirst)], 1)))
                tensorDist = tensorDist.pow(2.0).neg()
                tensorDist = (tensorDist - tensorDist.max(1, True)[0]).exp()
        
                tensorDivisor = tensorDist.sum(1, True).reciprocal()
        
                tensorScaleX = self.moduleScaleX(tensorDist * torch.nn.functional.unfold(input=tensorFlow[:, 0:1, :, :], 
                                                            kernel_size=self.intUnfold,
                                                            stride=1, 
                                                            padding=int((self.intUnfold - 1) / 2)).view_as(tensorDist)) * tensorDivisor
                tensorScaleY = self.moduleScaleY(tensorDist * torch.nn.functional.unfold(input=tensorFlow[:, 1:2, :, :], 
                                                            kernel_size=self.intUnfold,
                                                            stride=1, padding=int((self.intUnfold - 1) / 2)).view_as(tensorDist)) * tensorDivisor
        
                return torch.cat([tensorScaleX, tensorScaleY], 1)


        # end
        # end
        self.moduleFeatures = Features()
        self.moduleMatching = torch.nn.ModuleList([Matching(intLevel) for intLevel in [2, 3, 4, 5, 6]])
        self.moduleSubpixel = torch.nn.ModuleList([Subpixel(intLevel) for intLevel in [2, 3, 4, 5, 6]])
        self.moduleRegularization = torch.nn.ModuleList([Regularization(intLevel) for intLevel in [2, 3, 4, 5, 6]])
        #self.load_state_dict(torch.load(pretrain_model_path))


# end

    def forward(self, tensorFirst, tensorSecond):
        tensorFirst[:, 0, :, :] = tensorFirst[:, 0, :, :] - 0.411618
        tensorFirst[:, 1, :, :] = tensorFirst[:, 1, :, :] - 0.434631
        tensorFirst[:, 2, :, :] = tensorFirst[:, 2, :, :] - 0.454253

        tensorSecond[:, 0, :, :] = tensorSecond[:, 0, :, :] - 0.410782
        tensorSecond[:, 1, :, :] = tensorSecond[:, 1, :, :] - 0.433645
        tensorSecond[:, 2, :, :] = tensorSecond[:, 2, :, :] - 0.452793

        tensorFeaturesFirst = self.moduleFeatures(tensorFirst)
        tensorFeaturesSecond = self.moduleFeatures(tensorSecond)

        tensorFirst = [tensorFirst]
        tensorSecond = [tensorSecond]

        for intLevel in [1, 2, 3, 4, 5]:
            tensorFirst.append(torch.nn.functional.interpolate(input=tensorFirst[-1], size=(
            tensorFeaturesFirst[intLevel].size(2), tensorFeaturesFirst[intLevel].size(3)), mode='bilinear',
                                                               align_corners=False))
            tensorSecond.append(torch.nn.functional.interpolate(input=tensorSecond[-1], size=(
            tensorFeaturesSecond[intLevel].size(2), tensorFeaturesSecond[intLevel].size(3)), mode='bilinear',
                                                                align_corners=False))
        # end

        tensorFlow = None

        for intLevel in [-1, -2, -3, -4, -5]:
            tensorFlow = self.moduleMatching[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                       tensorFeaturesFirst[intLevel], tensorFeaturesSecond[intLevel],
                                                       tensorFlow)
            tensorFlow = self.moduleSubpixel[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                       tensorFeaturesFirst[intLevel], tensorFeaturesSecond[intLevel],
                                                       tensorFlow)
            tensorFlow = self.moduleRegularization[intLevel](tensorFirst[intLevel], tensorSecond[intLevel],
                                                             tensorFeaturesFirst[intLevel], tensorFeaturesSecond[intLevel],
                                                             tensorFlow)
        # end

        return tensorFlow * 20.0


    # end
    # end




##########################################################

def estimate(tensorFirst, tensorSecond, liteflow_Network):
    """
    :param tensorFirst: NCHW   normal float tensor
    :param tensorSecond: NCHW   normal float tensor
    :return:
    """
    assert (tensorFirst.size(2) == tensorSecond.size(2))
    assert (tensorFirst.size(3) == tensorSecond.size(3))

    intWidth = tensorFirst.size(3)
    intHeight = tensorFirst.size(2)

    # assert(intWidth == 1024) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue
    # assert(intHeight == 436) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue

    tensorPreprocessedFirst = tensorFirst#.cuda()
    tensorPreprocessedSecond = tensorSecond#.cuda()

    intPreprocessedWidth = int(math.floor(math.ceil(intWidth / 32.0) * 32.0))
    intPreprocessedHeight = int(math.floor(math.ceil(intHeight / 32.0) * 32.0))

    tensorPreprocessedFirst = torch.nn.functional.interpolate(input=tensorPreprocessedFirst,
                                                              size=(intPreprocessedHeight, intPreprocessedWidth),
                                                              mode='bilinear', align_corners=False)
    tensorPreprocessedSecond = torch.nn.functional.interpolate(input=tensorPreprocessedSecond,
                                                               size=(intPreprocessedHeight, intPreprocessedWidth),
                                                               mode='bilinear', align_corners=False)

    import time


    #moduleNetwork = Network().cuda().eval()
    start = time.time()
    with torch.no_grad():
        input = liteflow_Network(tensorPreprocessedFirst, tensorPreprocessedSecond)
        tensorFlow = torch.nn.functional.interpolate(input=input, 
                                                     size=(intHeight, intWidth),
                                                     mode='bilinear', 
                                                     align_corners=False)
    
    end = time.time()
    print('Inference time: {:.1f} ms'.format((end - start) * 1000.0))
    
    tensorFlow[:, 0, :, :] *= float(intWidth) / float(intPreprocessedWidth)
    tensorFlow[:, 1, :, :] *= float(intHeight) / float(intPreprocessedHeight)
    
    return tensorFlow.cpu()


# end
def estimate_single(tensorFirst, tensorSecond):
    assert (tensorFirst.size(1) == tensorSecond.size(1))
    assert (tensorFirst.size(2) == tensorSecond.size(2))

    intWidth = tensorFirst.size(2)
    intHeight = tensorFirst.size(1)

    # assert(intWidth == 1024) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue
    # assert(intHeight == 436) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue

    tensorPreprocessedFirst = tensorFirst.cuda().view(1, 3, intHeight, intWidth)
    tensorPreprocessedSecond = tensorSecond.cuda().view(1, 3, intHeight, intWidth)

    intPreprocessedWidth = int(math.floor(math.ceil(intWidth / 32.0) * 32.0))
    intPreprocessedHeight = int(math.floor(math.ceil(intHeight / 32.0) * 32.0))

    tensorPreprocessedFirst = torch.nn.functional.interpolate(input=tensorPreprocessedFirst,
                                                              size=(intPreprocessedHeight, intPreprocessedWidth),
                                                              mode='bilinear', align_corners=False)
    tensorPreprocessedSecond = torch.nn.functional.interpolate(input=tensorPreprocessedSecond,
                                                               size=(intPreprocessedHeight, intPreprocessedWidth),
                                                               mode='bilinear', align_corners=False)

    import time
    start = time.time()
    with torch.no_grad():
        tensorFlow = torch.nn.functional.interpolate(
            input=moduleNetwork_single(tensorPreprocessedFirst, tensorPreprocessedSecond), size=(intHeight, intWidth),
            mode='bilinear', align_corners=False)

    end = time.time()
    print('Inference time: {:.1f} ms'.format((end - start) * 1000.0))

    tensorFlow[:, 0, :, :] *= float(intWidth) / float(intPreprocessedWidth)
    tensorFlow[:, 1, :, :] *= float(intHeight) / float(intPreprocessedHeight)

    return tensorFlow[0, :, :, :].cpu()


# end
##########################################################
def liteflow(first_image_tensor, second_image_tensor, liteflow_Network):
    
    """
    first_image :  NCHW   0-255 float tensor  120_60
    second_image: NCHW  0-255 tensor   60
    return : NCHW  numpy  60
    """
    #moduleNetwork = Network().cuda().eval()
    #tensorFirst = torch.FloatTensor(numpy.array(first_image).astype(numpy.float32) * (1.0 / 255.0))
    #tensorSecond = torch.FloatTensor(numpy.array(second_image).astype(numpy.float32) * (1.0 / 255.0))
    tensorFirst = first_image_tensor * (1.0 / 255.0)
    tensorSecond = second_image_tensor * (1.0 / 255.0)
    
    assert (tensorFirst.size(2) == tensorSecond.size(2))
    assert (tensorFirst.size(3) == tensorSecond.size(3))

    tensorOutput = estimate(tensorFirst, tensorSecond, liteflow_Network)
   
    '''
    with open(arguments_strOut, 'wb') as objectOutput:
        numpy.array([80, 73, 69, 72], numpy.uint8).tofile(objectOutput)
        numpy.array([tensorOutput.size(2), tensorOutput.size(1)], numpy.int32).tofile(objectOutput)
        numpy.array(tensorOutput.numpy().transpose(2, 3, 1), numpy.float32).tofile(objectOutput)
    '''
    
    flow_uv = numpy.array(tensorOutput.numpy().transpose(0, 2, 3, 1), numpy.float32)
    # flow_color = flow_vis.flow_to_color(flow_uv, convert_to_bgr=True)
    # img1 = cv2.imread(arguments_strFirst)
    # height, width = flow_color.shape[0], flow_color.shape[1]
    intWidth = tensorFirst.size(3)
    intHeight = tensorFirst.size(2)
    grid_x, grid_y = numpy.meshgrid(numpy.arange(intWidth), numpy.arange(intHeight))
    grid_x = (grid_x - flow_uv[:, :, :, 0]).astype(numpy.float32)
    grid_y = (grid_y - flow_uv[:, :, :, 1]).astype(numpy.float32)

    # img120_60_warped = cv2.remap(img1, grid_x, grid_y, interpolation=cv2.INTER_LINEAR)
    # cv2.imwrite(arguments_strFirst.replace('.jpg', '_warped.jpg'), img2_warped)

    # Warp label
    # label_img = cv2.imread('images/f60_warped_undistorted.png', cv2.IMREAD_GRAYSCALE)
    # label_img_warped = cv2.remap(label_img, grid_x, grid_y, interpolation=cv2.INTER_NEAREST)
    # cv2.imwrite('images/f60_warped_undistorted_warped.png', label_img_warped)

    # Display the image
    '''
    cv2.imshow('flow', flow_color)
    cv2.waitKey(0)
    cv2.imwrite('./flow.png', flow_color)
    '''
    grid_120_60 = [grid_x, grid_y]
    grid_60_120 = [-1 * grid_x, -1 * grid_y]
    return grid_120_60, grid_60_120


if __name__ == '__main__':
    moduleNetwork_single = Network_single().cuda().eval()
    tensorFirst = torch.FloatTensor(
        numpy.array(PIL.Image.open(arguments_strFirst))[:, :, ::-1].transpose(2, 0, 1).astype(numpy.float32) * (
                1.0 / 255.0))
    tensorSecond = torch.FloatTensor(
        numpy.array(PIL.Image.open(arguments_strSecond))[:, :, ::-1].transpose(2, 0, 1).astype(numpy.float32) * (
                1.0 / 255.0))

    tensorOutput = estimate_single(tensorFirst, tensorSecond)

    with open(arguments_strOut, 'wb') as objectOutput:
        numpy.array([80, 73, 69, 72], numpy.uint8).tofile(objectOutput)
        numpy.array([tensorOutput.size(2), tensorOutput.size(1)], numpy.int32).tofile(objectOutput)
        numpy.array(tensorOutput.numpy().transpose(1, 2, 0), numpy.float32).tofile(objectOutput)
    flow_uv = numpy.array(tensorOutput.numpy().transpose(1, 2, 0), numpy.float32)

    # Apply the coloring (for OpenCV, set convert_to_bgr=True)
    flow_color = flow_vis.flow_to_color(flow_uv, convert_to_bgr=True)

    # Warp the first image to the second

    img1 = cv2.imread(arguments_strFirst)
    height, width = flow_color.shape[0], flow_color.shape[1]
    grid_x, grid_y = numpy.meshgrid(numpy.arange(width), numpy.arange(height))
    grid_x = (grid_x - flow_uv[:, :, 0]).astype(numpy.float32)
    grid_y = (grid_y - flow_uv[:, :, 1]).astype(numpy.float32)
    img2_warped = cv2.remap(img1, grid_x, grid_y, interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(arguments_strFirst.replace('.jpg', '_warped.jpg'), img2_warped)

    # Warp label
    # label_img = cv2.imread('images/f60_warped_undistorted.png', cv2.IMREAD_GRAYSCALE)
    # label_img_warped = cv2.remap(label_img, grid_x, grid_y, interpolation=cv2.INTER_NEAREST)
    # cv2.imwrite('images/f60_warped_undistorted_warped.png', label_img_warped)

    # Display the image
    cv2.imshow('flow', flow_color)
    cv2.waitKey(0)
    cv2.imwrite('./flow.png', flow_color)

# end